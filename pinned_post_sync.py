"""Движок закреплённого поста: отправка или правка, затем закреп — общий для catalog_bot и menu_bot.

PinnedPostSync приводит один пост к актуальному состоянию по записи в state (post_state):
  - пометку uncertain пишет на диск ДО отправки, чтобы прерванный запуск не продублировал
    пост; точный отказ Telegram пометку снимает, неизвестный исход оставляет;
  - если запрос прошёл, а запись о нём не сохранилась, — CRITICAL в лог с записью для
    ручного внесения (SyncOutcome.state_lost);
  - закреп идёт отдельным шагом: упал закреп — пост не дублируется, следующий запуск
    повторит только закреп.
Бот-наследник говорит только, как слать, править и закреплять, и подставляет свои тексты
(SyncTexts); итог каждого запуска — SyncOutcome, строки для вывода — format_*_line.
"""
from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace

from post_state import (
    STEP_EDIT,
    STEP_PIN,
    STEP_SEND,
    STEP_SKIP,
    STEP_UNCERTAIN,
    StateStore,
    make_entry,
    make_uncertain_entry,
    now_iso,
    plan_steps,
)
from telegram_sender import SendOutcomeUnknown

log = logging.getLogger("pinned_post_sync")

# Тексты итогов, одинаковые у каталога и меню; различающиеся — в SyncTexts.
NOTE_UNCHANGED = "без изменений"
NOTE_STATE_WRITE_FAILED = "не удалось записать state до отправки — ничего не отправлено"
NOTE_SEND_REFUSED = "Telegram отклонил отправку"
NOTE_MARKER_STUCK_SUFFIX = ", но пометку в state снять не удалось (CRITICAL в логе)"
NOTE_EDIT_REFUSED = "Telegram отклонил правку (пост удалён или уже такой же — причина в ERROR выше)"
NOTE_STATE_LOST = "state не сохранён — запись в логе (CRITICAL)"
NOTE_PIN_REFUSED = "закрепить не удалось (нужно право закреплять у бота) — pinned=false"


@dataclass(frozen=True)
class SyncTexts:
    """Тексты движка, зависящие от бота: подсказки владельцу и CRITICAL-записи лога.

    marker_release_critical принимает один %s (ключ поста), state_lost_critical — два
    (ключ поста и JSON записи для ручного внесения).
    """
    uncertain_hint: str
    send_unknown_note: str
    edit_unknown_note: str
    pin_unknown_note: str
    marker_release_critical: str
    state_lost_critical: str


@dataclass(frozen=True)
class SyncOutcome:
    """Итог обработки поста. stop=True — остальные посты трогать нельзя.

    requested=True — запрос к Telegram ушёл. state_lost=True — запрос прошёл,
    а запись о нём в state сохранить не удалось.
    """
    actions: list[str] = field(default_factory=list)
    message_id: int | None = None
    ok: bool = True
    stop: bool = False
    requested: bool = False
    state_lost: bool = False
    note: str = ""


class PinnedPostSync(ABC):
    """Приводит один пост к актуальному состоянию: отправка или правка, затем закреп.

    Наследник реализует четыре запроса к Telegram; всё остальное — защита от дубля,
    запись state, итоги — здесь и одинаково для всех ботов.
    """

    def __init__(self, store: StateStore, texts: SyncTexts):
        self.store = store
        self.texts = texts

    @abstractmethod
    def state_key(self, job) -> int:
        """Ключ записи поста в state (thread_id темы)."""

    @abstractmethod
    def request_send(self, job) -> int | None:
        """Отправка поста: message_id, None при точном отказе; SendOutcomeUnknown — исход неизвестен."""

    @abstractmethod
    def request_edit(self, message_id: int, job) -> int | None:
        """Правка поста: message_id, None при точном отказе; SendOutcomeUnknown — исход неизвестен."""

    @abstractmethod
    def request_pin(self, message_id: int) -> bool:
        """Закреп без уведомления: False при точном отказе; SendOutcomeUnknown — исход неизвестен."""

    def run(self, job) -> SyncOutcome:
        """job — любой объект с content_hash; остальное нужно только запросам наследника."""
        key = self.state_key(job)
        entry = self.store.get(key)
        steps = plan_steps(entry, job.content_hash)
        if steps[0] == STEP_UNCERTAIN:
            return SyncOutcome(actions=[STEP_UNCERTAIN], ok=False, note=self.texts.uncertain_hint)
        step_handler = {STEP_SEND: self._send, STEP_EDIT: self._edit, STEP_SKIP: self._keep}[steps[0]]
        outcome = step_handler(job, key, entry)
        if outcome.ok and STEP_PIN in steps:
            outcome = self._pin(key, outcome)
        return outcome

    def _keep(self, job, key: int, entry: dict) -> SyncOutcome:
        return SyncOutcome(actions=[STEP_SKIP], message_id=entry["message_id"], note=NOTE_UNCHANGED)

    def _send(self, job, key: int, entry: None) -> SyncOutcome:
        # Пометка идёт на диск ДО запроса: если процесс убьют (Ctrl+C, kill) посреди отправки,
        # следующий запуск увидит uncertain и не пошлёт пост второй раз.
        if not self.store.put(key, make_uncertain_entry()):
            return SyncOutcome(actions=[STEP_SEND], ok=False, stop=True, note=NOTE_STATE_WRITE_FAILED)
        try:
            message_id = self.request_send(job)
        except SendOutcomeUnknown:
            return SyncOutcome(actions=[STEP_SEND], ok=False, stop=True, requested=True,
                               note=self.texts.send_unknown_note)
        if message_id is None:
            return self._release_marker(key)
        # Настоящая запись заменяет пометку сразу: если упадёт закреп, повторной отправки не будет.
        return self._persist(key, make_entry(message_id, False, job.content_hash), [STEP_SEND])

    def _release_marker(self, key: int) -> SyncOutcome:
        """Точный отказ Telegram: пост не создан, пометку uncertain снимаем — пост снова «чистый»."""
        if self.store.remove(key):
            return SyncOutcome(actions=[STEP_SEND], ok=False, requested=True, note=NOTE_SEND_REFUSED)
        log.critical(self.texts.marker_release_critical, key)
        return SyncOutcome(actions=[STEP_SEND], ok=False, stop=True, requested=True,
                           note=NOTE_SEND_REFUSED + NOTE_MARKER_STUCK_SUFFIX)

    def _edit(self, job, key: int, entry: dict) -> SyncOutcome:
        message_id = entry["message_id"]
        try:
            edited_id = self.request_edit(message_id, job)
        except SendOutcomeUnknown:
            return SyncOutcome(actions=[STEP_EDIT], message_id=message_id, ok=False, stop=True,
                               requested=True, note=self.texts.edit_unknown_note)
        if edited_id is None:
            return SyncOutcome(actions=[STEP_EDIT], message_id=message_id, ok=False, requested=True,
                               note=NOTE_EDIT_REFUSED)
        updated = make_entry(message_id, bool(entry.get("pinned")), job.content_hash)
        return self._persist(key, updated, [STEP_EDIT])

    def _persist(self, key: int, entry: dict, actions: list[str]) -> SyncOutcome:
        """Сохраняет запись поста после успешного запроса; без state следующий запуск продублирует пост."""
        if self.store.put(key, entry):
            return SyncOutcome(actions=actions, message_id=entry["message_id"], requested=True)
        log.critical(self.texts.state_lost_critical, key, json.dumps(entry, ensure_ascii=False))
        return SyncOutcome(actions=actions, message_id=entry["message_id"], ok=False, stop=True,
                           requested=True, state_lost=True, note=NOTE_STATE_LOST)

    def _pin(self, key: int, outcome: SyncOutcome) -> SyncOutcome:
        actions = outcome.actions + [STEP_PIN]
        try:
            pinned = self.request_pin(outcome.message_id)
        except SendOutcomeUnknown:
            return replace(outcome, actions=actions, ok=False, requested=True,
                           note=self.texts.pin_unknown_note)
        if not pinned:
            return replace(outcome, actions=actions, ok=False, requested=True, note=NOTE_PIN_REFUSED)
        entry = {**self.store.get(key), "pinned": True, "updated_at": now_iso()}
        return self._persist(key, entry, actions)


def format_plan_line(label: str, steps: list[str], media_caption: str, media_path: str) -> str:
    """Строка dry-run: что будет сделано с постом и какой файл уйдёт шапкой."""
    return f"{label}: план {' + '.join(steps)}; {media_caption} {media_path}"


def format_outcome_line(label: str, outcome: SyncOutcome) -> str:
    message = f" message_id={outcome.message_id}" if outcome.message_id else ""
    status = "" if outcome.ok else "СБОЙ "
    note = f" — {outcome.note}" if outcome.note else ""
    return f"{label}: {status}{' + '.join(outcome.actions)}{message}{note}"
