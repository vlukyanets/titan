# ruff: noqa: RUF001, E501
"""Benchmark set for ADR 0014: fake notes and memories, and queries with the item each should find.

A third of the items each in English, Russian and Ukrainian. Every query names
its language and the item it should find; UNRELATED queries should find nothing.
"""

ITEMS = {
    # English
    "en-car": "Car service\n\nBook the oil change and tyre swap at the garage before the trip in May.",
    "en-dentist": "Dentist\n\nThe filling on the lower left tooth fell out, call the clinic on Monday.",
    "en-passport": "Renew passport\n\nThe passport expires in March, book an appointment at the office.",
    "en-garden": "Garden\n\nPlant tomatoes and basil once the frost is over, water every other evening.",
    "en-birthday": "Gift for Sam\n\nSam turns forty in June; he wanted a good chess set.",
    "en-wifi": "Home network\n\nThe router drops the connection at night, try another channel.",
    "en-recipe": "Pancakes\n\nTwo eggs, a cup of flour, a cup of milk, a pinch of salt, fry thin.",
    "en-run": "Running plan\n\nThree runs a week, a long one on Sunday, aim for a half marathon in autumn.",
    "en-tax": "Tax return\n\nCollect the receipts for the home office and send the return by April.",
    "en-book": "Reading\n\nFinish the novel about the lighthouse keeper before the book club meets.",
    "en-mem-coffee": "Prefers coffee black, without sugar.",
    "en-mem-allergy": "Is allergic to cats.",
    "en-mem-sister": "Has a sister called Mia who lives in Lisbon.",
    "en-mem-morning": "Likes to do focused work early in the morning.",
    # Russian
    "ru-boiler": "Котёл\n\nКотёл в подвале шумит и плохо греет, вызвать мастера до холодов.",
    "ru-vacation": "Отпуск\n\nВ августе поехать на море, забронировать жильё у пляжа заранее.",
    "ru-doctor": "Анализы\n\nСдать кровь натощак в четверг, результаты отнести терапевту.",
    "ru-english": "Английский\n\nЗаниматься по двадцать минут в день, повторять неправильные глаголы.",
    "ru-moving": "Переезд\n\nУпаковать книги в коробки, заказать грузовик на субботу.",
    "ru-bike": "Велосипед\n\nПоменять цепь и подкачать колёса перед сезоном.",
    "ru-budget": "Бюджет\n\nСократить траты на кафе, откладывать десятую часть зарплаты.",
    "ru-cat": "Кот\n\nКоту пора на прививку, ветклиника работает до семи.",
    "ru-kitchen": "Ремонт кухни\n\nВыбрать плитку и смеситель, мастер придёт во вторник.",
    "ru-mem-tea": "Любит зелёный чай с жасмином.",
    "ru-mem-vegetarian": "Не ест мясо уже три года.",
    "ru-mem-son": "Сын ходит на плавание по средам.",
    "ru-mem-night": "Плохо спит, если пьёт кофе после обеда.",
    # Ukrainian
    "uk-insurance": "Страховка\n\nПродовжити страховку на машину до кінця місяця.",
    "uk-concert": "Концерт\n\nКвитки на джазовий концерт у суботу, зустрітися біля входу о сьомій.",
    "uk-flowers": "Квіти\n\nПоливати орхідею раз на тиждень, не ставити на сонце.",
    "uk-gym": "Спортзал\n\nТренування з тренером у вівторок і четвер, взяти рушник.",
    "uk-laptop": "Ноутбук\n\nЗробити резервну копію фото і переставити систему.",
    "uk-grandma": "Бабуся\n\nПодзвонити бабусі в неділю, привезти ліки з аптеки.",
    "uk-course": "Курс\n\nЗакінчити онлайн-курс з фотографії до кінця кварталу.",
    "uk-rent": "Оренда\n\nСплатити оренду квартири до п'ятого числа.",
    "uk-shopping": "Покупки\n\nКупити молоко, хліб, яйця і каву на тиждень.",
    "uk-mem-wine": "Не п'є алкоголь.",
    "uk-mem-dog": "Має собаку на ім'я Бублик.",
    "uk-mem-language": "Розмовляє польською.",
    "uk-mem-evening": "Увечері любить гуляти парком.",
}

# (query, language, the item it should find)
QUERIES = [
    # Same language
    ("oil change for the car", "en", "en-car"),
    ("what does my sister do", "en", "en-mem-sister"),
    ("pancake recipe", "en", "en-recipe"),
    ("сантехник для котла", "ru", "ru-boiler"),
    ("сдать анализ крови", "ru", "ru-doctor"),
    ("подготовка велосипеда к сезону", "ru", "ru-bike"),
    ("коли платити за квартиру", "uk", "uk-rent"),
    ("резервна копія фотографій", "uk", "uk-laptop"),
    ("що купити в магазині", "uk", "uk-shopping"),
    ("how often to water the orchid", "en", "uk-flowers"),
    # Another language
    ("зубной врач", "ru", "en-dentist"),
    ("стоматолог пломба", "uk", "en-dentist"),
    ("документы для поездки за границу", "ru", "en-passport"),
    ("посадити помідори", "uk", "en-garden"),
    ("подарок на день рождения", "ru", "en-birthday"),
    ("інтернет пропадає вночі", "uk", "en-wifi"),
    ("подготовка к полумарафону", "ru", "en-run"),
    ("податкова декларація", "uk", "en-tax"),
    ("книжный клуб", "ru", "en-book"),
    ("як я п'ю каву", "uk", "en-mem-coffee"),
    ("аллергия на животных", "ru", "en-mem-allergy"),
    ("когда мне лучше работать", "ru", "en-mem-morning"),
    ("seaside holiday in summer", "en", "ru-vacation"),
    ("learning English vocabulary", "en", "ru-english"),
    ("переїзд у нову квартиру", "uk", "ru-moving"),
    ("saving money", "en", "ru-budget"),
    ("vaccination for my pet", "en", "ru-cat"),
    ("new tiles for the kitchen", "en", "ru-kitchen"),
    ("favourite drink", "en", "ru-mem-tea"),
    ("чи їсть він м'ясо", "uk", "ru-mem-vegetarian"),
    ("swimming lessons for my son", "en", "ru-mem-son"),
    ("why can't I sleep", "en", "ru-mem-night"),
    ("car insurance renewal", "en", "uk-insurance"),
    ("билеты на концерт", "ru", "uk-concert"),
    ("workout with a personal trainer", "en", "uk-gym"),
    ("позвонить бабушке", "ru", "uk-grandma"),
    ("photography course", "en", "uk-course"),
    ("does he drink wine", "en", "uk-mem-wine"),
    ("как зовут собаку", "ru", "uk-mem-dog"),
    ("what languages do I speak", "en", "uk-mem-language"),
    ("вечерняя прогулка", "ru", "uk-mem-evening"),
    ("buy milk and bread", "en", "uk-shopping"),
]

UNRELATED = [
    "quantum physics lecture",
    "рецепт борща",
    "футбольний матч",
    "weather tomorrow",
    "история Древнего Рима",
    "ремонт даху",
]


def language(item: str) -> str:
    return item.split("-", 1)[0]
