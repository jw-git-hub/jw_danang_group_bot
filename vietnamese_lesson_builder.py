#!/usr/bin/env python3
"""
Генератор записей для vietnamese_lessons.json.

Запускается ВРУЧНУЮ для пополнения курса партиями по месяцам.
НЕ предназначен для cron.

Источники материала:
  - Wikibooks Vietnamese (https://en.wikibooks.org/wiki/Vietnamese) — CC BY-SA
  - Расширение/адаптация через `claude -p`

Структура курса: 12 месяцев × 30 дней = 360 + 5 дополнительных дней
для 12-го месяца (всего 365 уроков).

Запуск:
    python3 vietnamese_lesson_builder.py --month N [--preview] [--force] [--allow-published]
                                          [--day N[,N...]] [--limit N]

  --month N            обязателен, 1..12
  --preview            не сохраняет JSON, печатает результат в stdout
  --force              перезаписать существующие уроки месяца (уже опубликованные
                        дни — см. current_day в vietnamese_state.json — пропускаются,
                        если не передан --allow-published)
  --allow-published     вместе с --force перезаписать и уже опубликованные дни
  --day N[,N...]        сгенерировать только перечисленные дни месяца
  --limit N             ограничить число генераций (для теста)

Параллельные запуски билдера (например, несколько месяцев одновременно) сериализуются
через файловый лок vietnamese_lessons.json.lock — вторая копия сразу завершится с
ошибкой, а не тихо потеряет уроки первой.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("vn_lesson_builder")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
LESSONS_PATH = BASE_DIR / "vietnamese_lessons.json"
LESSONS_LOCK_PATH = LESSONS_PATH.with_name(LESSONS_PATH.name + ".lock")
STATE_PATH = BASE_DIR / "vietnamese_state.json"
CACHE_DIR = BASE_DIR / "cache" / "wikibooks"
CACHE_TTL_SECONDS = 7 * 24 * 3600  # 7 дней

# Сколько раз переспросить Claude, если ответ не распарсился или урок невалиден
MAX_ATTEMPTS = 3

# После скольких подряд неудачных запусков `claude -p` (rc != 0) прерывать весь
# запуск билдера — если исчерпан лимит аккаунта, месячный прогон иначе сделает
# ещё ~90 обречённых вызовов подряд.
MAX_CONSECUTIVE_RC_FAILURES = 3

# Лимит длины отрендеренного поста в UTF-16 code units (так Telegram считает длину
# сообщения; см. vietnamese_bot._utf16_len) — с запасом от TELEGRAM_MAX_LEN=4096.
MAX_POST_UTF16_LEN = 3800

# ---------------------------------------------------------------------------
# Месячная структура курса
# ---------------------------------------------------------------------------
MONTH_THEMES = {
    1: "базы, приветствия, вежливость, числа 1-100",
    2: "кафе и ресторан",
    3: "транспорт и навигация",
    4: "рынок, торг, шопинг",
    5: "жильё, аренда, бытовые проблемы",
    6: "здоровье, аптека, врач",
    7: "работа, бизнес, документы",
    8: "культура, праздники",
    9: "семья, отношения, знакомства",
    10: "эмоции, чувства, описания",
    11: "время, природа, погода",
    12: "сленг, идиомы, культурные нюансы",
}

# Темы по дням. Месяцы 1-11 — по 30, месяц 12 — 35 (дополнительные).
LESSON_TOPICS: dict[int, list[str]] = {
    1: [
        "Xin chào (Здравствуйте / Привет)",
        "Cảm ơn (Спасибо)",
        "Xin lỗi (Извините)",
        "Tạm biệt (До свидания)",
        "Có (Да)",
        "Không (Нет)",
        "Tôi tên là... (Меня зовут...)",
        "Bạn khỏe không? (Как дела?)",
        "Tôi không hiểu (Я не понимаю)",
        "Bạn nói tiếng Anh không? (Вы говорите по-английски?)",
        "Rất vui được gặp bạn (Очень рад познакомиться)",
        "Tôi đến từ Nga (Я из России)",
        "Bạn tên là gì? (Как тебя зовут?)",
        "Một, hai, ba (Один, два, три)",
        "Bốn, năm, sáu (Четыре, пять, шесть)",
        "Bảy, tám, chín, mười (Семь, восемь, девять, десять)",
        "Mười một đến hai mươi (11-20)",
        "Ba mươi, bốn mươi, năm mươi (30, 40, 50)",
        "Một trăm (Сто)",
        "Vâng / Dạ (Да — вежливое)",
        "Làm ơn (Пожалуйста — просьба)",
        "Không có gì (Не за что)",
        "Chúc mừng (Поздравляю)",
        "Chào buổi sáng (Доброе утро)",
        "Chào buổi tối (Добрый вечер)",
        "Hẹn gặp lại (До встречи)",
        "Tôi xin lỗi, tôi muộn (Извините, я опоздал)",
        "Bạn bao nhiêu tuổi? (Сколько тебе лет?)",
        "Bạn sống ở đâu? (Где ты живёшь?)",
        "Tôi sống ở Đà Nẵng (Я живу в Дананге)",
    ],
    2: [
        "Cho tôi xem thực đơn (Дайте, пожалуйста, меню)",
        "Một bàn cho hai người (Столик на двоих)",
        "Tôi muốn gọi món (Я хочу заказать)",
        "Cái này là gì? (Что это?)",
        "Có cay không? (Это острое?)",
        "Không cay (Не острое, пожалуйста)",
        "Cho tôi một cà phê sữa đá (Кофе со льдом и молоком)",
        "Phở bò (Фо с говядиной)",
        "Bún chả (Бун ча)",
        "Cơm gà (Рис с курицей)",
        "Bánh mì (Бань ми)",
        "Một chai bia (Бутылку пива)",
        "Nước lọc (Вода без газа)",
        "Tính tiền (Счёт, пожалуйста)",
        "Có wifi không? (Есть Wi-Fi?)",
        "Mật khẩu wifi là gì? (Какой пароль от Wi-Fi?)",
        "Ngon quá! (Очень вкусно!)",
        "Tôi no rồi (Я наелся)",
        "Không có hành (Без лука)",
        "Không có đường (Без сахара)",
        "Cho tôi thêm đá (Ещё льда, пожалуйста)",
        "Tôi ăn chay (Я вегетарианец)",
        "Có món chay không? (Есть вегетарианская еда?)",
        "Cho tôi mang về (На вынос, пожалуйста)",
        "Đắt quá (Слишком дорого)",
        "Có giảm giá không? (Есть скидка?)",
        "Tôi dị ứng với hải sản (У меня аллергия на морепродукты)",
        "Cho tôi đũa (Дайте палочки)",
        "Cho tôi muỗng / dĩa (Дайте ложку / вилку)",
        "Cảm ơn, rất ngon (Спасибо, было очень вкусно)",
    ],
    3: [
        "Đi taxi (Поехать на такси)",
        "Cho tôi đi đến... (Отвезите меня в...)",
        "Bao xa? (Как далеко?)",
        "Bao lâu? (Сколько времени ехать?)",
        "Bao nhiêu tiền? (Сколько стоит?)",
        "Bật đồng hồ (Включите счётчик)",
        "Dừng ở đây (Остановите здесь)",
        "Rẽ trái (Поверните налево)",
        "Rẽ phải (Поверните направо)",
        "Đi thẳng (Прямо)",
        "Quay lại (Развернитесь)",
        "Tôi bị lạc đường (Я заблудился)",
        "Đường này tên gì? (Как называется эта улица?)",
        "Sân bay ở đâu? (Где аэропорт?)",
        "Bến xe buýt ở đâu? (Где автобусная остановка?)",
        "Vé xe buýt bao nhiêu? (Сколько стоит билет?)",
        "Xe máy thuê ở đâu? (Где арендовать байк?)",
        "Một ngày bao nhiêu? (Сколько в день?)",
        "Đổ đầy xăng (Полный бак, пожалуйста)",
        "Bản đồ (Карта)",
        "Gần đây có... không? (Поблизости есть...?)",
        "ATM ở đâu? (Где банкомат?)",
        "Trạm xăng (Заправка)",
        "Đi bộ được không? (Можно дойти пешком?)",
        "Grab (Grab — приложение такси)",
        "Tài xế ơi (Эй, водитель — обращение)",
        "Chậm thôi (Помедленнее)",
        "Tôi vội (Я спешу)",
        "Cầu Rồng (Мост Дракон)",
        "Bãi biển Mỹ Khê (Пляж Мишеу)",
    ],
    4: [
        "Nhiêu vậy chị? / Bao nhiêu một ký? (Почём? Сколько за килограмм? — разговорный рыночный вариант)",
        "Đắt quá (Слишком дорого)",
        "Hai trăm nghìn được không chị? (Двести тысяч — идёт? Назвать свою цену при торге)",
        "Cuối cùng giá bao nhiêu? (Окончательная цена?)",
        "Tôi mua (Я беру)",
        "Tôi không mua (Я не беру)",
        "Cho tôi xem cái kia (Покажите вон то)",
        "Có cái khác không? (Есть другое?)",
        "Cái này bằng vải gì? (Из какой это ткани / материала?)",
        "Cỡ lớn hơn (Размер побольше)",
        "Cỡ nhỏ hơn (Размер поменьше)",
        "Thử được không? (Можно примерить?)",
        "Cái này chật quá (Мне жмёт / тесновато)",
        "Không vừa, cho tôi đổi cái khác được không? (Не подошло — можно обменять?)",
        "Hàng giả (Подделка)",
        "Có bảo hành không? (Есть гарантия?)",
        "Trái cây (Фрукты)",
        "Chị cân lại giúp em với (Перевесьте, пожалуйста — контроль веса на рынке)",
        "Cho tôi nửa ký (Полкило, пожалуйста)",
        "Tươi không? (Свежее?)",
        "Tôi chỉ xem thôi (Я просто смотрю)",
        "Có tiền lẻ không? (Есть мелочь?)",
        "Trả bằng thẻ được không? (Можно картой?)",
        "Tiền mặt (Наличные)",
        "Đổi tiền (Обмен валюты)",
        "Cho tôi tờ nhỏ hơn (Дайте купюрами помельче)",
        "Đồng (Донги)",
        "Mua hai tặng một (Два по цене одного)",
        "Hôm nay khuyến mãi (Сегодня акция)",
        "Cho tôi cái túi (Дайте пакет)",
    ],
    5: [
        "Tôi muốn thuê căn hộ (Хочу снять квартиру)",
        "Một tháng bao nhiêu? (Сколько в месяц?)",
        "Đặt cọc bao nhiêu? (Какой депозит?)",
        "Bao gồm điện nước không? (Включены ли электричество и вода?)",
        "Có máy lạnh không? (Есть кондиционер?)",
        "Có máy giặt không? (Есть стиральная машина?)",
        "Có chỗ để xe máy không? (Есть место для парковки байка?)",
        "Có thang máy không? (Есть лифт?)",
        "Tôi muốn xem phòng (Хочу посмотреть квартиру)",
        "Hợp đồng bao lâu? (На какой срок договор?)",
        "Hoá đơn điện (Счёт за электричество)",
        "Nước không chảy (Не течёт вода)",
        "Điện bị cúp (Отключили электричество)",
        "Wifi không hoạt động (Не работает Wi-Fi)",
        "Máy lạnh hỏng rồi (Кондиционер сломался)",
        "Cần sửa chữa (Нужен ремонт)",
        "Gọi thợ điện (Вызовите электрика)",
        "Gọi thợ ống nước (Вызовите сантехника)",
        "Khoá bị hỏng (Замок сломан)",
        "Tôi mất chìa khoá (Я потерял ключ)",
        "Hàng xóm ồn quá (Соседи шумят)",
        "Có gián / chuột (Тараканы / мыши)",
        "Tủ lạnh không lạnh (Холодильник не морозит)",
        "Cần dọn dẹp (Нужна уборка)",
        "Đổ rác ở đâu? (Где выбрасывать мусор?)",
        "Khi nào lấy rác? (Когда забирают мусор?)",
        "Bảo vệ (Охрана)",
        "Chủ nhà (Хозяин квартиры)",
        "Chuyển đi (Съезжать)",
        "Trả phòng (Сдать квартиру)",
    ],
    6: [
        "Tôi bị ốm (Я заболел)",
        "Tôi bị sốt (У меня температура)",
        "Tôi bị đau đầu (Голова болит)",
        "Tôi bị đau bụng (Живот болит)",
        "Tôi bị tiêu chảy (У меня диарея)",
        "Tôi bị cảm (Я простудился)",
        "Tôi bị ho (У меня кашель)",
        "Tôi bị dị ứng (У меня аллергия)",
        "Tôi cần đi bác sĩ (Мне нужен врач)",
        "Bệnh viện ở đâu? (Где больница?)",
        "Hiệu thuốc gần nhất (Ближайшая аптека)",
        "Có thuốc giảm đau không? (Есть обезболивающее?)",
        "Thuốc kháng sinh (Антибиотики)",
        "Đau ở đây (Болит здесь)",
        "Tôi bị thương (Я поранился)",
        "Cần băng cứu thương (Нужны бинты)",
        "Tôi cần cấp cứu (Мне нужна скорая помощь)",
        "Gọi xe cấp cứu (Вызовите скорую)",
        "Bảo hiểm y tế (Медицинская страховка)",
        "Tôi không thể ăn được (Я не могу есть)",
        "Tôi không ngủ được (Я не могу спать)",
        "Mệt mỏi (Усталость)",
        "Chóng mặt (Головокружение)",
        "Buồn nôn (Тошнота)",
        "Bị muỗi đốt (Укус комара)",
        "Bị say nắng (Солнечный удар)",
        "Cháy nắng (Сгорел на солнце)",
        "Bị ngộ độc thực phẩm (Пищевое отравление)",
        "Một ngày uống mấy lần? (Сколько раз в день принимать?)",
        "Trước khi ăn / sau khi ăn (До / после еды)",
    ],
    7: [
        "Tôi làm việc ở... (Я работаю в...)",
        "Tôi là freelancer (Я фрилансер)",
        "Văn phòng (Офис)",
        "Họp (Совещание)",
        "Khách hàng (Клиент)",
        "Đồng nghiệp (Коллега)",
        "Sếp (Начальник)",
        "Lương bao nhiêu? (Какая зарплата?)",
        "Hợp đồng lao động (Трудовой договор)",
        "Visa làm việc (Рабочая виза)",
        "Giấy phép lao động (Разрешение на работу)",
        "Thẻ tạm trú (TRC — карта временного проживания)",
        "Hộ chiếu (Паспорт)",
        "Đại sứ quán Nga (Российское посольство)",
        "Cục xuất nhập cảnh (Иммиграционная служба)",
        "Gia hạn visa (Продление визы)",
        "Mã số thuế (Налоговый код)",
        "Hóa đơn VAT (НДС-счёт)",
        "Mở tài khoản ngân hàng (Открыть банковский счёт)",
        "Chuyển khoản (Банковский перевод)",
        "Gửi tiền về Nga (Отправить деньги в Россию)",
        "Hợp đồng (Договор)",
        "Ký tên (Подписать)",
        "Công ty (Компания)",
        "Doanh nghiệp (Бизнес)",
        "Đăng ký kinh doanh (Регистрация бизнеса)",
        "Khởi nghiệp (Стартап)",
        "Đầu tư (Инвестиции)",
        "Báo cáo (Отчёт)",
        "Hạn cuối (Дедлайн)",
    ],
    8: [
        "Tết Nguyên Đán (Тет — Новый год по лунному календарю)",
        "Chúc mừng năm mới (С Новым годом)",
        "Lì xì (Конверт с деньгами на Тет)",
        "Bánh chưng (Рисовый пирог на Тет)",
        "Trung thu (Праздник середины осени)",
        "Bánh trung thu (Лунный пирог)",
        "Lễ Vu Lan (Праздник почитания родителей)",
        "Giỗ Tổ Hùng Vương (День предков-Хунгов)",
        "Quốc khánh (День независимости)",
        "Áo dài (Аозай — национальное платье)",
        "Nón lá (Конусная шляпа)",
        "Chùa (Буддийский храм)",
        "Đền (Храм-ден)",
        "Phong thủy (Фэн-шуй)",
        "Cúng tổ tiên (Поклонение предкам)",
        "Bàn thờ (Алтарь)",
        "Hương / nhang (Благовония)",
        "Lễ chùa (Посещение храма)",
        "Cầu may (Просить удачи)",
        "Năm mới may mắn (Удачи в новом году)",
        "Lễ hội pháo hoa (Фестиваль фейерверков в Дананге)",
        "Cầu Vàng (Золотой мост на Бана-Хиллс)",
        "Đám cưới (Свадьба)",
        "Đám tang (Похороны)",
        "Sinh nhật (День рождения)",
        "Chúc mừng sinh nhật (С днём рождения)",
        "Lễ tình nhân (День Святого Валентина)",
        "Giáng sinh (Рождество)",
        "Karaoke (Караоке)",
        "Văn hoá Việt Nam (Вьетнамская культура)",
    ],
    9: [
        "Gia đình tôi (Моя семья)",
        "Bố / cha (Папа)",
        "Mẹ (Мама)",
        "Anh trai (Старший брат)",
        "Em gái (Младшая сестра)",
        "Ông / bà (Дедушка / бабушка)",
        "Vợ (Жена)",
        "Chồng (Муж)",
        "Con trai / con gái (Сын / дочь)",
        "Bạn trai / bạn gái (Парень / девушка)",
        "Người yêu (Любимый/любимая)",
        "Bạn thân (Близкий друг)",
        "Anh / em — обращение к мужчине",
        "Chị / em — обращение к женщине",
        "Cô / chú — тётя / дядя",
        "Bạn có người yêu chưa? (У тебя есть пара?)",
        "Tôi độc thân (Я не в отношениях)",
        "Tôi đã kết hôn (Я женат / замужем)",
        "Tôi có con (У меня есть дети)",
        "Bạn có mấy anh chị em? (Сколько у тебя братьев и сестёр?)",
        "Sinh nhật bạn ngày nào? (Когда твой день рождения?)",
        "Bạn làm nghề gì? (Кем работаешь?)",
        "Sở thích của bạn là gì? (Какое у тебя хобби?)",
        "Tôi thích bạn (Ты мне нравишься)",
        "Tôi yêu em (Я люблю тебя)",
        "Mình hẹn hò nhé (Давай встречаться)",
        "Đi chơi không? (Пойдём гулять?)",
        "Đi cà phê không? (Пойдём в кафе?)",
        "Bạn đẹp lắm (Ты очень красивая)",
        "Cho mình số điện thoại (Дай номер телефона)",
    ],
    10: [
        "Tôi vui (Я рад)",
        "Tôi buồn (Мне грустно)",
        "Tôi mệt (Я устал)",
        "Tôi giận (Я злюсь)",
        "Tôi sợ (Я боюсь)",
        "Tôi thất vọng (Я разочарован)",
        "Tôi nhớ nhà (Я скучаю по дому)",
        "Tôi cô đơn (Мне одиноко)",
        "Tôi yêu Đà Nẵng (Я люблю Дананг)",
        "Tôi ghét... (Я ненавижу...)",
        "Đẹp (Красивый)",
        "Xấu (Некрасивый)",
        "Cao / thấp (Высокий / низкий)",
        "To / nhỏ (Большой / маленький)",
        "Nhanh / chậm (Быстрый / медленный)",
        "Nóng / lạnh (Горячий / холодный)",
        "Mới / cũ (Новый / старый)",
        "Tốt / xấu (Хороший / плохой)",
        "Khó / dễ (Трудный / лёгкий)",
        "Đắt / rẻ (Дорогой / дешёвый)",
        "Ngon / dở (Вкусный / невкусный)",
        "Vui vẻ (Весёлый)",
        "Hiền lành (Добрый)",
        "Thông minh (Умный)",
        "Lười (Ленивый)",
        "Chăm chỉ (Трудолюбивый)",
        "Nhiệt tình (Энергичный, страстный)",
        "Tôi cảm thấy... (Я чувствую...)",
        "Bình tĩnh (Спокойный)",
        "Hồi hộp (Волнуюсь)",
    ],
    11: [
        "Hôm nay (Сегодня)",
        "Hôm qua (Вчера)",
        "Ngày mai (Завтра)",
        "Mấy giờ rồi? (Который час?)",
        "Bây giờ là... (Сейчас...)",
        "Buổi sáng / chiều / tối (Утро / день / вечер)",
        "Đêm khuya (Поздняя ночь)",
        "Tuần này / tuần sau (Эта неделя / следующая)",
        "Tháng này / tháng sau (Этот месяц / следующий)",
        "Năm nay / năm sau (В этом году / в следующем)",
        "Thứ Hai đến Chủ Nhật (Понедельник — воскресенье)",
        "Tháng một đến tháng mười hai (Январь — декабрь)",
        "Trời nóng (Жарко)",
        "Trời lạnh (Холодно)",
        "Trời mưa (Идёт дождь)",
        "Trời nắng (Солнечно)",
        "Có gió (Ветрено)",
        "Bão (Тайфун)",
        "Mùa mưa (Сезон дождей)",
        "Mùa khô (Сухой сезон)",
        "Biển (Море)",
        "Núi (Горы)",
        "Sông (Река)",
        "Rừng (Лес)",
        "Cây (Дерево)",
        "Hoa (Цветок)",
        "Mặt trời (Солнце)",
        "Mặt trăng (Луна)",
        "Sao (Звезда)",
        "Đẹp trời (Хорошая погода)",
    ],
    12: [
        "Trời ơi! (О боже! — универсальное восклицание)",
        "Quá xá / quá trời (Очень / страшно — усилитель)",
        "Chất! (Круто! — молодёжный сленг)",
        "Đỉnh (Топ, бомба)",
        "Khét lẹt (Невероятно крутой)",
        "Hết hồn (Перепугался до смерти)",
        "Thôi rồi! (Всё, конец! — драматично)",
        "Đi đi! (Иди отсюда! — может быть и игриво)",
        "Em ơi (Эй, девушка! — стандартное обращение)",
        "Anh ơi (Эй, мужчина!)",
        "Mặn (Солёный — в смысле едкий, токсичный)",
        "Ngọt (Сладкий — иногда о приятном)",
        "Cay (Острый — о ситуации, не еде)",
        "Bao đẹp / bao ngon (Гарантированно красивый / вкусный)",
        "Không sao (Ничего, всё нормально)",
        "Ok luôn (Окей, само собой)",
        "Dí dỏm (С чувством юмора)",
        "Lầy (Расхлябанный, забавный)",
        "Quẩy (Зажигать, тусить)",
        "Ăn vạ (Притворяться обиженным)",
        "Mất gốc (Потерял корни — о вьете за границей)",
        "Sống ảo (Жить в соцсетях)",
        "Crush (Краш — заимствование)",
        "Có duyên / vô duyên (С обаянием / без манер)",
        "Lì xì điện tử (Цифровой Тет-конверт)",
        "Một chín một mười (Девять к десяти — почти равны)",
        "Cá nằm trên thớt (Рыба на разделочной доске — в безвыходной ситуации)",
        "Chó treo mèo đậy (Собаку повесь, кошку прикрой — будь начеку)",
        "Tre già măng mọc (Старый бамбук — молодой росток — преемственность поколений)",
        "Ăn cơm chưa? (Ты поел? — народное приветствие)",
        "Người ngoài (Чужак, не свой)",
        "Mất mặt (Потерять лицо)",
        "Giữ thể diện (Сохранить лицо)",
        "Nể (Уважать, считаться)",
        "Tình cảm (Душевность, эмоциональность)",
    ],
}

# Доп.хештеги по месяцам (к базовым 4)
MONTH_TAGS = {
    1: ["#приветствие", "#базовое"],
    2: ["#еда", "#ресторан"],
    3: ["#транспорт", "#такси"],
    4: ["#рынок", "#шопинг"],
    5: ["#жильё", "#аренда"],
    6: ["#здоровье", "#аптека"],
    7: ["#работа", "#документы"],
    8: ["#культура", "#праздники"],
    9: ["#семья", "#знакомства"],
    10: ["#эмоции", "#описания"],
    11: ["#время", "#погода"],
    12: ["#сленг", "#идиомы"],
}

# Подсказки Wikibooks: какой раздел листать для какой темы месяца.
# URL может не существовать — это нормально, builder обрабатывает 404.
WIKIBOOKS_HINTS = {
    1: ["https://en.wikibooks.org/wiki/Vietnamese/Greetings",
        "https://en.wikibooks.org/wiki/Vietnamese/Numbers",
        "https://en.wikibooks.org/wiki/Vietnamese"],
    2: [],
    3: [],
    4: ["https://en.wikibooks.org/wiki/Vietnamese/Numbers"],
    5: ["https://en.wikibooks.org/wiki/Vietnamese/House"],
    6: [],
    7: [],
    8: [],
    9: ["https://en.wikibooks.org/wiki/Vietnamese/Family"],
    10: ["https://en.wikibooks.org/wiki/Vietnamese/Adjectives"],
    11: ["https://en.wikibooks.org/wiki/Vietnamese/Dates_and_times"],
    12: [],
}

# ---------------------------------------------------------------------------
# Helpers: диапазон дней по месяцу
# ---------------------------------------------------------------------------
def month_to_day_range(month: int) -> range:
    """1 → 1..30, ..., 11 → 301..330, 12 → 331..365 (35 дней)."""
    if not 1 <= month <= 12:
        raise ValueError(f"month должен быть 1..12, получено {month}")
    if month < 12:
        start = (month - 1) * 30 + 1
        return range(start, start + 30)
    return range(331, 366)  # 331..365


# ---------------------------------------------------------------------------
# Wikibooks: загрузка с кэшем
# ---------------------------------------------------------------------------
def _cache_path_for_url(url: str) -> Path:
    """https://en.wikibooks.org/wiki/Vietnamese/Greetings → cache/wikibooks/Vietnamese_Greetings.html"""
    name = url.rsplit("/wiki/", 1)[-1].replace("/", "_")
    name = re.sub(r"[^A-Za-z0-9_\-]", "_", name) or "wikibooks_page"
    return CACHE_DIR / f"{name}.html"


def fetch_wikibooks_page(url: str) -> Optional[str]:
    """Возвращает HTML-страницу, кэширует на 7 дней. None при ошибке/404."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path_for_url(url)

    if path.exists():
        age = time.time() - path.stat().st_mtime
        if age < CACHE_TTL_SECONDS:
            try:
                return path.read_text(encoding="utf-8")
            except OSError as e:
                log.warning("Кэш не читается (%s): %s", path, e)

    try:
        resp = requests.get(
            url,
            headers={"User-Agent": "DanangBot/1.0 (Wikibooks lesson builder)"},
            timeout=20,
        )
    except requests.RequestException as e:
        log.warning("Wikibooks fetch failed: %s — %s", url, e)
        return None

    if resp.status_code != 200:
        log.info("Wikibooks %s → HTTP %d (пропускаем)", url, resp.status_code)
        return None

    try:
        path.write_text(resp.text, encoding="utf-8")
    except OSError as e:
        log.warning("Не удалось закэшировать %s: %s", path, e)

    return resp.text


def extract_wikibooks_excerpt(html: str, topic_hint: str, max_chars: int = 1500) -> str:
    """Достаёт кусок текста, релевантный ключевым словам из topic_hint."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")

    # Грубо: соберём все параграфы и таблицы
    chunks = []
    for tag in soup.select("p, li, td"):
        text = tag.get_text(" ", strip=True)
        if 30 <= len(text) <= 400:
            chunks.append(text)

    if not chunks:
        return ""

    # Рейтинг по совпадению ключевых слов
    keywords = [k.lower() for k in re.findall(r"[A-Za-zĐđ]+", topic_hint) if len(k) > 2]
    if not keywords:
        keywords = ["vietnamese"]

    scored = []
    for c in chunks:
        cl = c.lower()
        score = sum(1 for k in keywords if k in cl)
        if score > 0:
            scored.append((score, c))

    scored.sort(reverse=True)
    if not scored:
        # Если ничего не подошло — берём первые два параграфа
        return "\n\n".join(chunks[:2])[:max_chars]

    out = []
    total = 0
    for _, c in scored:
        if total + len(c) > max_chars:
            break
        out.append(c)
        total += len(c)
    return "\n\n".join(out)


def find_wikibooks_excerpt(month: int, topic_hint: str) -> tuple[str, Optional[str]]:
    """Перебирает подсказки по месяцу, возвращает (excerpt, url)."""
    for url in WIKIBOOKS_HINTS.get(month, []):
        html = fetch_wikibooks_page(url)
        if not html:
            continue
        excerpt = extract_wikibooks_excerpt(html, topic_hint)
        if excerpt:
            return excerpt, url
    return "", None


# ---------------------------------------------------------------------------
# Claude: генерация одного урока
# ---------------------------------------------------------------------------
CLAUDE_PROMPT_TEMPLATE = """Ты — преподаватель вьетнамского языка для русскоязычных экспатов в Дананге.

Сгенерируй ОДИН урок на тему: "{lesson_topic}"
(Месяц курса: {month_theme})

Требования к уроку:
- Реально употребительная фраза, которую экспат услышит/скажет в Дананге
- Кириллическая транскрипция русскими буквами (НЕ IPA, не латиница)
- Подробный пословный разбор каждого слова
- Контекст использования: где, с кем, в какой ситуации
- Подсказка по тонам — какой тон у каждого слова, частые ошибки русскоязычных
- Длина "context" — 2-4 предложения

Опционально используй материал Wikibooks (приведу ниже), но ты можешь его дополнять и адаптировать.

Ответ ТОЛЬКО в формате JSON одним объектом:
{{
  "vietnamese": "...",
  "transliteration_ru": "...",
  "translation_ru": "...",
  "breakdown": [{{"word": "...", "transliteration": "...", "meaning": "..."}}],
  "context": "...",
  "tone_tip": "..."
}}

Никаких пояснений вне JSON. Никаких markdown блоков ```json. Только сырой JSON.

{wikibooks_excerpt_if_any}
"""


def _strip_code_fence(text: str) -> str:
    """Убираем возможные ```json ... ``` обёртки."""
    text = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        return fence.group(1).strip()
    return text


def _extract_json_object(text: str) -> Optional[str]:
    """Ищет первый сбалансированный {...} в тексте."""
    depth = 0
    start = -1
    in_string = False
    escape = False
    for i, ch in enumerate(text):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                return text[start:i + 1]
    return None


_consecutive_rc_failures = 0


def call_claude(prompt: str, timeout: int = 240) -> Optional[dict]:
    """Запускает claude -p и пытается распарсить JSON. Возвращает dict, и ТОЛЬКО dict —
    валидный, но не-объектный JSON (список, строка, число) считается неудачей, как и
    отсутствие JSON вовсе.

    --strict-mcp-config и --tools "" отключают MCP-серверы и все инструменты, а
    cwd=tempfile.gettempdir() уводит запуск из каталога проекта — так проектный/глобальный
    CLAUDE.md и allow-листы разрешений на этот вызов не действуют."""
    global _consecutive_rc_failures
    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--strict-mcp-config", "--tools", ""],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=tempfile.gettempdir(),
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        log.error("claude -p таймаут (%ds)", timeout)
        return None
    except FileNotFoundError:
        log.error("Не найдена команда 'claude'. Установите Claude Code CLI.")
        return None
    except Exception as e:
        log.error("Ошибка запуска claude: %s", e)
        return None

    if result.returncode != 0:
        stdout_tail = (result.stdout or "")[-500:]
        stderr_tail = (result.stderr or "")[-500:]
        log.error(
            "claude -p rc=%d\nstdout (хвост, ≤500 симв.): %s\nstderr (хвост, ≤500 симв.): %s",
            result.returncode, stdout_tail, stderr_tail,
        )
        _consecutive_rc_failures += 1
        if _consecutive_rc_failures >= MAX_CONSECUTIVE_RC_FAILURES:
            log.error(
                "%d запусков claude -p подряд завершились с rc != 0 — похоже, исчерпан лимит "
                "аккаунта. Прерываем весь запуск билдера",
                _consecutive_rc_failures,
            )
            sys.exit(1)
        return None

    _consecutive_rc_failures = 0

    raw = _strip_code_fence(result.stdout or "")
    if not raw:
        log.warning("Пустой stdout от claude")
        return None

    # Прямой парсинг
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        return data
    if data is not None:
        log.warning("Ответ Claude — валидный JSON, но не объект (%s)", type(data).__name__)

    # Через regex/баланс скобок
    candidate = _extract_json_object(raw)
    if candidate:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError as e:
            log.warning("JSON не парсится после извлечения: %s. Превью: %.200s", e, candidate)
            return None
        if isinstance(data, dict):
            return data
        log.warning(
            "Извлечённый JSON — не объект (%s). Превью: %.200s", type(data).__name__, candidate,
        )
        return None

    log.warning("В ответе Claude не найден JSON-объект. Превью: %.200s", raw)
    return None


# ---------------------------------------------------------------------------
# Сборка урока (заполнение служебных полей)
# ---------------------------------------------------------------------------
def build_lesson_record(
    *,
    day: int,
    month: int,
    claude_data: dict,
    wikibooks_url: Optional[str],
    used_wikibooks: bool,
) -> dict:
    base_tags = [
        "#вьетнамский",
        f"#урок{day}",
        "#Дананг",
        "#жизньвоВьетнаме",
        "#Vietnam",
    ]
    extra = MONTH_TAGS.get(month, [])
    tags = base_tags + extra

    if used_wikibooks and wikibooks_url:
        source = "wikibooks+claude"
    else:
        source = "claude"

    # claude_data приходит от call_claude, который теперь гарантирует dict, но значения
    # ПОЛЕЙ внутри него — произвольны (Claude мог прислать null, число, список вместо
    # строки). str(x or "") не даёт упасть на None/не-строке, а `or []` вместо голого
    # .get(..., []) не спасает от НЕ-list значения (breakdown: "нет" осталось бы строкой) —
    # поэтому breakdown отдельно проверяется через isinstance.
    breakdown_raw = claude_data.get("breakdown")
    breakdown = breakdown_raw if isinstance(breakdown_raw, list) else []

    record = {
        "day": day,
        "month_block": month,
        "vietnamese": str(claude_data.get("vietnamese") or "").strip(),
        "transliteration_ru": str(claude_data.get("transliteration_ru") or "").strip(),
        "translation_ru": str(claude_data.get("translation_ru") or "").strip(),
        "breakdown": breakdown,
        "context": str(claude_data.get("context") or "").strip(),
        "tone_tip": str(claude_data.get("tone_tip") or "").strip(),
        "source": source,
        "wikibooks_url": wikibooks_url or "",
        "tags": tags,
    }
    return record


REQUIRED_FIELDS = [
    "vietnamese", "transliteration_ru", "translation_ru",
    "breakdown", "context", "tone_tip",
]

# Текстовые поля урока, которые целиком идут в пост (для missing_fields и content_defects).
_TEXT_FIELDS = ("vietnamese", "transliteration_ru", "translation_ru", "context", "tone_tip")
# Обязательные подполя одного элемента breakdown — именно так называет их код (build_lesson_record
# берёт их из claude_data["breakdown"], формат задан в CLAUDE_PROMPT_TEMPLATE).
_BREAKDOWN_FIELDS = ("word", "transliteration", "meaning")


def _is_nonempty_str(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _valid_breakdown(value) -> bool:
    """breakdown валиден только как непустой список словарей с непустыми строковыми
    word/transliteration/meaning в каждом — что угодно другое (null, строка, список строк,
    словарь без нужных ключей) не считается заполненным полем."""
    if not isinstance(value, list) or not value:
        return False
    for item in value:
        if not isinstance(item, dict):
            return False
        if not all(_is_nonempty_str(item.get(k)) for k in _BREAKDOWN_FIELDS):
            return False
    return True


def missing_fields(record: dict) -> list[str]:
    """Список обязательных полей, которые пусты, отсутствуют или неправильного типа."""
    out = []
    for k in REQUIRED_FIELDS:
        if k == "breakdown":
            if not _valid_breakdown(record.get(k)):
                out.append(k)
        elif not _is_nonempty_str(record.get(k)):
            out.append(k)
    return out


def lesson_is_valid(record: dict) -> bool:
    """Минимальные требования к качеству урока."""
    missing = missing_fields(record)
    if missing:
        log.warning("Урок day=%s: пустые поля %s", record.get("day"), ", ".join(missing))
        return False
    return True


# ---------------------------------------------------------------------------
# content_defects: самокоррекция/служебный мусор вместо чистого содержимого
# ---------------------------------------------------------------------------
# Инцидент дня 103: модель прислала "...нет, này тоже huyền. Вот исправленный вариант" —
# JSON был валиден и все поля заполнены, missing_fields пропустил бы это как готовый урок.
_DEFECT_ELLIPSIS_NO = re.compile(r"(?:\.\.\.|…)\s*нет\b", re.IGNORECASE)
_DEFECT_SELF_CORRECTION = re.compile(
    r"исправленн\w* вариант|исправлю|исправляю|```|\bjson\b|as an ai|"
    r"i cannot|i can't|here is|\b(?:tone_tip|transliteration_ru|translation_ru|breakdown)\b",
    re.IGNORECASE,
)
_DEFECT_LINE_START = re.compile(r"^\s*(?:Вот|Конечно|Here|Sure)\b", re.MULTILINE)

# Игнорируем при определении "алфавита" символа: комбинирующие знаки (в т.ч. ударение),
# цифры, пунктуацию, пробелы и управляющие символы.
_IGNORED_UNICODE_CATEGORIES = ("M", "N", "P", "Z", "C")


def _char_alphabet(ch: str) -> Optional[str]:
    """Грубое определение 'алфавита' символа по первому слову его Unicode-имени
    (CYRILLIC/LATIN/GEORGIAN/HIRAGANA/...). None — если символ не буква конкретного
    алфавита (цифра/пунктуация/пробел/комбинирующий знак)."""
    if unicodedata.category(ch)[0] in _IGNORED_UNICODE_CATEGORIES:
        return None
    name = unicodedata.name(ch, "")
    if not name:
        return None
    return name.split(" ", 1)[0]


def _mixed_script_tokens(text: str) -> list[str]:
    """Токены (через пробел) кириллической транскрипции, содержащие буквы НЕ кириллицы —
    Georgian/Hiragana/IPA-заимствования из Latin и т.п. Действует только если в поле в
    целом есть кириллица (то есть это и правда транскрипция, а не что-то ещё)."""
    field_alphabets = {a for a in (_char_alphabet(ch) for ch in text) if a}
    if "CYRILLIC" not in field_alphabets or len(field_alphabets) <= 1:
        return []
    bad = []
    for token in text.split():
        token_alphabets = {a for a in (_char_alphabet(ch) for ch in token) if a}
        if any(a != "CYRILLIC" for a in token_alphabets):
            bad.append(token)
    return bad


def content_defects(record: dict) -> list[str]:
    """Признаки того, что текстовые поля урока содержат самокоррекцию/служебный мусор
    вместо чистого содержимого, или что кириллическая транскрипция засорена буквами
    другого алфавита. В отличие от missing_fields, здесь поля формально непустые —
    проблема в СОДЕРЖИМОМ. Список пуст ⇔ дефектов не найдено."""
    defects: list[str] = []

    def check_text(label: str, value) -> None:
        if not isinstance(value, str) or not value:
            return
        if _DEFECT_ELLIPSIS_NO.search(value):
            defects.append(f"{label}: похоже на самокоррекцию модели («…нет»)")
        m = _DEFECT_SELF_CORRECTION.search(value)
        if m:
            defects.append(f"{label}: служебный/самокоррекционный текст ({m.group(0)!r})")
        if _DEFECT_LINE_START.search(value):
            defects.append(f"{label}: строка начинается как ответ ассистента, а не контент урока")

    def check_transliteration(label: str, value) -> None:
        if not isinstance(value, str) or not value:
            return
        for token in _mixed_script_tokens(value):
            defects.append(f"{label}: транскрипция смешивает кириллицу с другим алфавитом («{token}»)")

    for field in _TEXT_FIELDS:
        check_text(field, record.get(field))
    check_transliteration("transliteration_ru", record.get("transliteration_ru"))

    breakdown = record.get("breakdown")
    if isinstance(breakdown, list):
        for idx, item in enumerate(breakdown):
            if not isinstance(item, dict):
                continue
            for bf in _BREAKDOWN_FIELDS:
                check_text(f"breakdown[{idx}].{bf}", item.get(bf))
            check_transliteration(f"breakdown[{idx}].transliteration", item.get("transliteration"))

    return defects


# ---------------------------------------------------------------------------
# Проверка длины отрендеренного поста (лимит Telegram)
# ---------------------------------------------------------------------------
def _fallback_render_text(record: dict) -> str:
    """Грубая, заведомо НЕ заниженная реконструкция текста поста — используется, если
    vietnamese_bot.format_post недоступен (например, файл сейчас правит другой
    исполнитель, и он временно не импортируется). Блоки разделены пустой строкой
    везде (реальный формат местами компактнее) и секция Wikibooks добавлена всегда —
    так оценка не может оказаться МЕНЬШЕ настоящей длины."""
    breakdown = record.get("breakdown")
    breakdown_lines = []
    if isinstance(breakdown, list):
        for item in breakdown:
            if not isinstance(item, dict):
                continue
            breakdown_lines.append(
                "• {} ({}) — {}".format(
                    item.get("word", ""), item.get("transliteration", ""), item.get("meaning", ""),
                )
            )
    tags = record.get("tags")
    tags_line = " ".join(tags) if isinstance(tags, list) else ""

    blocks = [
        f"🇻🇳 УРОК ВЬЕТНАМСКОГО — День {record.get('day')} / 365",
        f"📌 Фраза: {record.get('vietnamese', '')}",
        f"🔊 Транскрипция: {record.get('transliteration_ru', '')}",
        f"🇷🇺 Перевод: {record.get('translation_ru', '')}",
        "📖 Разбор:\n" + "\n".join(breakdown_lines),
        "💬 Когда использовать:\n" + str(record.get("context", "")),
        "🎵 Тон-лайфхак:\n" + str(record.get("tone_tip", "")),
        "📚 По материалам Wikibooks (CC BY-SA)",
        tags_line,
    ]
    return "\n\n".join(b for b in blocks if b)


def _render_lesson_text(record: dict) -> str:
    """Рендерит пост ТОЧНО так же, как его увидят подписчики — через настоящий
    vietnamese_bot.format_post, если модуль сейчас можно импортировать без побочных
    эффектов (импорт — лениво, внутри функции: vietnamese_bot.py — чужой файл, и если
    он в моменте не импортируется, это не должно ронять билдер). Иначе — консервативная
    локальная оценка."""
    try:
        from vietnamese_bot import format_post
        return format_post(record, False)
    except Exception as e:
        log.debug("vietnamese_bot.format_post недоступен (%s) — консервативная оценка длины", e)
        return _fallback_render_text(record)


def post_too_long(record: dict) -> tuple[bool, int]:
    """(превышен ли лимит, длина в UTF-16 code units — так же, как считает сам Telegram)."""
    length = len(_render_lesson_text(record).encode("utf-16-le")) // 2
    return length > MAX_POST_UTF16_LEN, length


# ---------------------------------------------------------------------------
# I/O для vietnamese_lessons.json
# ---------------------------------------------------------------------------
def load_lessons() -> list[dict]:
    if not LESSONS_PATH.exists():
        return []
    try:
        with open(LESSONS_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        log.error("vietnamese_lessons.json повреждён: %s — отказываюсь записывать", e)
        sys.exit(1)

    if not isinstance(data, list):
        # Раньше здесь тихо возвращался [] — save_lessons_atomic следом переписал бы файл
        # только что сгенерированными уроками, а всё остальное содержимое было бы потеряно.
        log.error(
            "vietnamese_lessons.json: верхний уровень не массив (%s) — отказываюсь "
            "перезаписывать, нужна ручная проверка файла", type(data).__name__,
        )
        sys.exit(1)
    return data


def save_lessons_atomic(lessons: list[dict]) -> None:
    tmp = LESSONS_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(lessons, f, ensure_ascii=False, indent=2)
    os.replace(tmp, LESSONS_PATH)
    log.info("Сохранено: %s (%d уроков)", LESSONS_PATH, len(lessons))


def upsert_lesson(lessons: list[dict], record: dict) -> list[dict]:
    """Возвращает обновлённый список с заменой по day."""
    out = [l for l in lessons if l.get("day") != record["day"]]
    out.append(record)
    out.sort(key=lambda x: x.get("day", 0))
    return out


# ---------------------------------------------------------------------------
# Лок против параллельных запусков билдера
# ---------------------------------------------------------------------------
def acquire_lessons_lock(lock_path: Path):
    """Эксклюзивный неблокирующий fcntl-лок на всё время main(). Без него два билдера,
    запущенные параллельно (например, на разные месяцы одновременно), читают
    vietnamese_lessons.json независимо в начале работы и, сохраняя после КАЖДОГО урока,
    каждый раз переписывают файл своей веткой списка в памяти — уроки, сохранённые другим
    процессом между этими чтениями, тихо теряются. Лок держится открытым до конца
    процесса — ОС снимает его автоматически при завершении, даже при аварийном выходе."""
    lock_file = open(lock_path, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.error(
            "Не удалось стартовать: другой запуск билдера уже работает (занят лок %s) — "
            "выходим, чтобы не потерять уроки друг друга",
            lock_path,
        )
        sys.exit(1)
    return lock_file


def _read_current_day(state_path: Path) -> Optional[int]:
    """Читает current_day из vietnamese_state.json. ТОЛЬКО чтение — этим файлом управляет
    vietnamese_bot.py, билдер его не меняет. Нужен, чтобы --force не перезаписывал уже
    опубликованные дни (см. generate_for_month). Любая проблема с state-файлом (нет
    файла, битый JSON, не тот тип верхнего уровня/current_day) — не повод падать: просто
    считаем, что опубликованных уроков нет, с явным предупреждением в лог."""
    if not state_path.exists():
        log.warning(
            "%s не найден — считаем, что опубликованных уроков ещё нет", state_path,
        )
        return None
    try:
        with open(state_path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.warning("%s не читается (%s) — считаем, что опубликованных уроков нет", state_path, e)
        return None
    if not isinstance(data, dict):
        log.warning(
            "%s: верхний уровень не объект — считаем, что опубликованных уроков нет", state_path,
        )
        return None
    day = data.get("current_day")
    if not isinstance(day, int) or isinstance(day, bool):
        log.warning(
            "%s: current_day=%r невалиден — считаем, что опубликованных уроков нет",
            state_path, day,
        )
        return None
    return day


# ---------------------------------------------------------------------------
# Главная логика
# ---------------------------------------------------------------------------
def generate_for_month(
    month: int,
    *,
    preview: bool,
    force: bool,
    limit: Optional[int],
    allow_published: bool = False,
    only_days: Optional[set[int]] = None,
) -> list[dict]:
    full_days = list(month_to_day_range(month))
    topics = LESSON_TOPICS.get(month, [])
    if len(topics) < len(full_days):
        log.warning("Для месяца %d тем %d, дней %d — некоторые дни будут без специфической темы",
                    month, len(topics), len(full_days))
    # Тема дня определяется позицией дня ВНУТРИ МЕСЯЦА, а не позицией в (возможно
    # отфильтрованном через --day) списке ниже — иначе --day перепутал бы темы дней.
    topic_by_day = {
        d: (topics[i] if i < len(topics) else f"Тема дня {d} (месяц {month})")
        for i, d in enumerate(full_days)
    }

    days = full_days
    if only_days is not None:
        unknown = sorted(only_days - set(full_days))
        if unknown:
            log.warning("--day: дни %s вне диапазона месяца %d (%d..%d) — игнорируются",
                        unknown, month, full_days[0], full_days[-1])
        days = [d for d in full_days if d in only_days]
        if not days:
            log.error("--day: ни один из указанных дней не входит в месяц %d", month)
            return []

    # С --force перезаписываются только ЕЩЁ НЕ опубликованные дни (day >= current_day из
    # state) — иначе --force для месяца, где часть дней уже прочитана подписчиками и
    # вручную поправлена (см. историю правок 2026-08-21), стирает эти правки при
    # перегенерации всего месяца. --allow-published снимает это ограничение явно.
    published_before: Optional[int] = None
    if force and not allow_published:
        published_before = _read_current_day(STATE_PATH)
        if published_before is not None:
            log.info(
                "--force: дни < %d считаются уже опубликованными и будут пропущены "
                "(--allow-published перезапишет и их)",
                published_before,
            )

    existing = load_lessons()
    existing_by_day = {l.get("day"): l for l in existing}

    log.info("=== Месяц %d: %s ===", month, MONTH_THEMES.get(month, ""))
    log.info("Дни месяца: %d..%d (всего %d, к генерации в этом запуске: %d)",
              full_days[0], full_days[-1], len(full_days), len(days))
    log.info("Существующих уроков в JSON: %d", len(existing))

    generated: list[dict] = []
    produced = 0

    for day in days:
        if limit is not None and produced >= limit:
            log.info("Достигнут --limit=%d, остановка", limit)
            break

        if day in existing_by_day and not force:
            log.info("Урок %d уже есть — пропуск (используйте --force для перезаписи)", day)
            continue

        if force and published_before is not None and day < published_before:
            log.info(
                "Урок %d уже опубликован (current_day=%d в state) — пропуск даже с --force "
                "(используйте --allow-published, чтобы перезаписать)",
                day, published_before,
            )
            continue

        topic = topic_by_day[day]
        log.info("Урок %d/%d: «%s»", day, full_days[-1], topic)

        excerpt, wb_url = find_wikibooks_excerpt(month, topic)
        used_wikibooks = bool(excerpt)
        if used_wikibooks:
            log.info("  Wikibooks: %s (%d симв.)", wb_url, len(excerpt))
        else:
            log.info("  Wikibooks: не найдено — генерируем только через Claude")

        excerpt_block = f"Материал Wikibooks (CC BY-SA):\n{excerpt}" if excerpt else ""

        prompt = CLAUDE_PROMPT_TEMPLATE.format(
            lesson_topic=topic,
            month_theme=MONTH_THEMES.get(month, ""),
            wikibooks_excerpt_if_any=excerpt_block,
        )

        record = None
        retry_hint = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            claude_data = call_claude(prompt + retry_hint)
            if not claude_data:
                log.warning("Урок %d: попытка %d/%d — Claude не дал валидный JSON-объект",
                            day, attempt, MAX_ATTEMPTS)
                continue

            candidate = build_lesson_record(
                day=day,
                month=month,
                claude_data=claude_data,
                wikibooks_url=wb_url,
                used_wikibooks=used_wikibooks,
            )

            missing = missing_fields(candidate)
            if missing:
                log.warning("Урок %d: попытка %d/%d — не заполнены поля: %s",
                            day, attempt, MAX_ATTEMPTS, ", ".join(missing))
                retry_hint = (
                    "\n\nВАЖНО: в прошлый раз ты не заполнил обязательные поля: "
                    + ", ".join(missing)
                    + ". Верни JSON со ВСЕМИ полями схемы, ни одно не должно быть пустым "
                      "или отсутствовать."
                )
                continue

            # Поля формально заполнены, но могли прийти с самокоррекцией/служебным мусором
            # (инцидент дня 103: "...нет, này тоже huyền. Вот исправленный вариант") или с
            # транскрипцией, засорённой буквами другого алфавита — missing_fields это не ловит.
            defects = content_defects(candidate)
            if defects:
                log.warning("Урок %d: попытка %d/%d — дефекты содержимого: %s",
                            day, attempt, MAX_ATTEMPTS, "; ".join(defects))
                retry_hint = (
                    "\n\nВАЖНО: прошлый ответ содержал брак вместо чистого содержимого урока: "
                    + "; ".join(defects)
                    + ". Верни ТОЛЬКО один финальный чистый JSON без самокоррекций, объяснений, "
                      "markdown-обёрток и служебных фраз; кириллическая транскрипция должна "
                      "быть только кириллицей."
                )
                continue

            too_long, length = post_too_long(candidate)
            if too_long:
                log.warning(
                    "Урок %d: попытка %d/%d — пост длиннее лимита (%d > %d UTF-16 code units)",
                    day, attempt, MAX_ATTEMPTS, length, MAX_POST_UTF16_LEN,
                )
                retry_hint = (
                    "\n\nВАЖНО: получившийся пост слишком длинный — сократи текст (особенно "
                    "context и tone_tip), сохранив структуру JSON и смысл."
                )
                continue

            record = candidate
            break

        if record is None:
            log.warning("Урок %d: %d попыток исчерпано — пропуск", day, MAX_ATTEMPTS)
            continue

        log.info("Урок %d сгенерирован: %s", day, record["vietnamese"])
        generated.append(record)
        produced += 1

        if not preview:
            # Перечитываем файл заново ПРЯМО ПЕРЕД сохранением, а не переиспользуем `existing`,
            # накопленный с начала запуска — так правки, внесённые вручную или другим
            # процессом, пока этот урок генерировался (это может занимать минуты), не
            # затираются полным перезаписыванием файла версией из памяти.
            existing = upsert_lesson(load_lessons(), record)
            save_lessons_atomic(existing)
            existing_by_day = {l.get("day"): l for l in existing}

    return generated


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Генератор уроков для vietnamese_lessons.json",
    )
    parser.add_argument("--month", type=int, required=True, help="Месяц курса 1..12")
    parser.add_argument("--preview", action="store_true",
                        help="Печать в stdout, не сохранять JSON")
    parser.add_argument("--force", action="store_true",
                        help="Перезаписать существующие уроки месяца (уже опубликованные "
                             "дни пропускаются — см. --allow-published)")
    parser.add_argument("--allow-published", action="store_true",
                        help="Вместе с --force перезаписать и уже опубликованные дни "
                             "(current_day из vietnamese_state.json)")
    parser.add_argument("--day", type=str, default=None,
                        help="Сгенерировать только перечисленные дни месяца: --day 5 или --day 5,12,13")
    parser.add_argument("--limit", type=int, default=None,
                        help="Ограничить число генераций (для теста)")
    args = parser.parse_args()

    if not 1 <= args.month <= 12:
        log.error("--month должен быть 1..12, получено %s", args.month)
        return 2

    only_days: Optional[set[int]] = None
    if args.day:
        try:
            only_days = {int(x.strip()) for x in args.day.split(",") if x.strip()}
        except ValueError:
            log.error("--day: не удалось разобрать список дней %r (ожидается N или N,M,...)", args.day)
            return 2
        if not only_days:
            log.error("--day: пустой список дней")
            return 2

    # Лок держим на протяжении всего main(): вторая параллельно запущенная копия билдера
    # (например, на другой месяц) читала бы тот же vietnamese_lessons.json независимо и
    # своей записью затёрла бы уроки, уже сохранённые этим процессом (см. acquire_lessons_lock).
    _lock_fh = acquire_lessons_lock(LESSONS_LOCK_PATH)  # noqa: F841 — держим ссылку, лок жив, пока жив файл

    log.info("=== Vietnamese lesson builder ===")
    log.info(
        "month=%d, preview=%s, force=%s, allow_published=%s, day=%s, limit=%s",
        args.month, args.preview, args.force, args.allow_published,
        sorted(only_days) if only_days else None, args.limit,
    )

    generated = generate_for_month(
        args.month,
        preview=args.preview,
        force=args.force,
        limit=args.limit,
        allow_published=args.allow_published,
        only_days=only_days,
    )

    log.info("Итого сгенерировано: %d", len(generated))

    if args.preview:
        print("=" * 70)
        print(json.dumps(generated, ensure_ascii=False, indent=2))
        print("=" * 70)
        log.info("Preview-режим: файл vietnamese_lessons.json НЕ изменён")
    else:
        log.info("Сохранено в %s", LESSONS_PATH)

    return 0 if generated or args.limit == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
