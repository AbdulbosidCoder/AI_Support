# База знаний v1: экраны Xonsaroy Pay

Источник истины: [`screens.json`](screens.json). Здесь краткий индекс. Персональные данные (телефон, имя, логин, SMS-коды) на снимках замаскированы.


## Регистрация и вход

| Экран | Страница | Состояние | Ошибка / что видно | Эскалация |
|---|---|---|---|---|
| [auth_phone_empty](../../data/screenshots/01_registration/auth_phone_empty.jpg) | `auth/phone` · Регистрация/вход, шаг 1 из 5: ввод номера телефона | empty | Поле номера пустое, кнопка KEYINGI неактивна (серая). | нет |
| [auth_phone_filled](../../data/screenshots/01_registration/auth_phone_filled.jpg) | `auth/phone` · Регистрация/вход, шаг 1 из 5: ввод номера телефона | normal | Ошибки нет: номер введён, кнопка активна. | нет |
| [auth_phone_error_camera_permission](../../data/screenshots/01_registration/auth_phone_error_camera_permission.jpg) | `auth/phone` · Регистрация/вход, шаг 1 из 5 | error | Ilova uchun kameradan foydalanishga ruxsat berilmagan. | если: Разрешение выдано, а ошибка остаётся. |
| [auth_myid_loading](../../data/screenshots/01_registration/auth_myid_loading.jpg) | `auth/myid` · Идентификация MyID | loading | Экран MyID с индикатором загрузки. Нормально, если длится несколько секунд; проблема, если загрузка бесконечная. | если: Загрузка бесконечная после перезапуска и смены сети. |
| [auth_sms_code_loading](../../data/screenshots/01_registration/auth_sms_code_loading.jpg) | `auth/otp-verify` · Регистрация/вход, шаг 2 из 5: SMS-код | loading | Код введён (6 символов, могут быть буквы и цифры), идёт проверка. | если: SMS не приходит после нескольких повторных запросов. |
| [auth_fingerprint_setup](../../data/screenshots/01_registration/auth_fingerprint_setup.jpg) | `auth/biometric-setup` · Регистрация, шаг 5 из 5: отпечаток пальца | normal | Ошибки нет. Последний шаг: включить вход по отпечатку пальца. | нет |

## Главная

| Экран | Страница | Состояние | Ошибка / что видно | Эскалация |
|---|---|---|---|---|
| [home_no_cards](../../data/screenshots/02_home/home_no_cards.jpg) | `home/dashboard` · Главная (Asosiy), нет карт | empty | К аккаунту не привязана ни одна карта. | нет |
| [home_scrolled](../../data/screenshots/02_home/home_scrolled.jpg) | `home/dashboard` · Главная (Asosiy), прокрутка вниз | normal | Ошибки нет. | нет |
| [home_error_no_payment_methods](../../data/screenshots/02_home/home_error_no_payment_methods.jpg) | `home/dashboard` · Главная (Asosiy) | error | To'lov vositalari yetarli emas | если: Карта уже добавлена, а сообщение появляется. |

## Карты

| Экран | Страница | Состояние | Ошибка / что видно | Эскалация |
|---|---|---|---|---|
| [card_add_form](../../data/screenshots/03_cards/card_add_form.jpg) | `cards/add` · Добавление карты (Yangi karta) | normal | Ошибки нет. Форма добавления карты. | нет |
| [card_add_sms_code_empty](../../data/screenshots/03_cards/card_add_sms_code_empty.jpg) | `cards/add-otp` · Добавление карты: SMS-код | empty | Ожидание SMS-кода для привязки карты. | если: SMS-информирование подключено к правильному номеру, а код не приходит. |
| [card_add_sms_code_partial](../../data/screenshots/03_cards/card_add_sms_code_partial.jpg) | `cards/add-otp` · Добавление карты: SMS-код | normal | Код вводится (часть цифр введена). | нет |
| [card_blocked_security_1h](../../data/screenshots/03_cards/card_blocked_security_1h.jpg) | `cards/balance-visibility` · Umumiy balansda ko'rsatish (карты в общем балансе) | error | Karta xavfsizlik maqsadida 1 soatga bloklangan | да |

## P2P-переводы

| Экран | Страница | Состояние | Ошибка / что видно | Эскалация |
|---|---|---|---|---|
| [transfers_main](../../data/screenshots/04_transfers/transfers_main.jpg) | `transfers/main` · Переводы (O'tkazmalar) | normal | Ошибки нет. Главный экран переводов. | нет |

## Платежи

| Экран | Страница | Состояние | Ошибка / что видно | Эскалация |
|---|---|---|---|---|
| [payments_main_top](../../data/screenshots/05_payments/payments_main_top.jpg) | `payments/main` · Платежи (To'lov), верх | normal | Ошибки нет. | нет |
| [payments_main_scrolled](../../data/screenshots/05_payments/payments_main_scrolled.jpg) | `payments/main` · Платежи (To'lov), категории услуг | normal | Ошибки нет. | нет |
| [services_list](../../data/screenshots/05_payments/services_list.jpg) | `payments/services` · Услуги (Xizmatlar) | normal | Ошибки нет. Отдельный экран услуг (открыт с главной). | нет |
| [category_aloqa_mobile](../../data/screenshots/05_payments/category_aloqa_mobile.jpg) | `payments/category/mobile` · Категория «Aloqa» (связь) | normal | Ошибки нет. | нет |
| [provider_steam_error_maintenance](../../data/screenshots/05_payments/provider_steam_error_maintenance.jpg) | `payments/provider-form` · Форма оплаты поставщика (Steam) | error | Ведутся профилактические работы | если: Клиент сообщает, что деньги списались. |
| [search_empty](../../data/screenshots/05_payments/search_empty.jpg) | `search` · Поиск (Qidiruv) | empty | Ошибки нет. Пустой поиск. | нет |

## История операций

| Экран | Страница | Состояние | Ошибка / что видно | Эскалация |
|---|---|---|---|---|
| [history_empty](../../data/screenshots/06_history/history_empty.jpg) | `history` · История (Tarix) | empty | За выбранный месяц операций нет. | если: Клиент говорит, что операция была, а в истории её нет. |

## Ошибки и ответы бота

### Ilova uchun kameradan foydalanishga ruxsat berilmagan.
Экран: `auth/phone`. Ошибка значит «Приложению не разрешено использовать камеру». Камера нужна для идентификации через MyID.

Шаги:
1. Откройте Настройки телефона → Приложения → Xonsaroy Pay → Разрешения.
2. Разрешите доступ к «Камере».
3. Вернитесь в приложение и повторите шаг.
4. Если ошибка повторяется, полностью закройте и снова откройте приложение.

### To'lov vositalari yetarli emas
Экран: `home/dashboard`. Сообщение значит «Недостаточно платёжных средств». На этом экране видно, что карт 0, поэтому операцию не с чего оплатить.

Шаги:
1. Добавьте карту Uzcard или Humo: «Kartalarim» → «Qo'shish».
2. Подтвердите добавление кодом из SMS.
3. Повторите операцию.

### Karta xavfsizlik maqsadida 1 soatga bloklangan
Экран: `cards/balance-visibility`. Сообщение значит «Карта заблокирована на 1 час в целях безопасности». Это временное ограничение в приложении, срок указан самим приложением.

Шаги:
1. Не пытайтесь повторно добавлять карту или проводить операции по ней, пока действует ограничение.
2. Попробуйте снова после окончания срока, указанного в приложении.
3. Если ограничение не снялось, напишите нам, и мы передадим обращение ответственному сотруднику.

Нельзя: Не называть причины и критерии блокировки (Antifraud). Не обещать разблокировку и не разблокировать. Не называть срок от себя: только «1 soat» как написано в приложении.

### Ведутся профилактические работы
Экран: `payments/provider-form`. Сообщение значит, что на стороне сервиса идут технические работы, и оплата этому поставщику сейчас недоступна.

Шаги:
1. Не повторяйте оплату много раз подряд.
2. Попробуйте позже.
3. Проверьте в «Tarix» (История), не было ли списания.
4. Если деньги списались, а услуга не оплачена, пришлите время операции и сумму (без номера карты), мы передадим на проверку.

Нельзя: Не называть время окончания работ. Не подтверждать и не опровергать списание без данных backend.

