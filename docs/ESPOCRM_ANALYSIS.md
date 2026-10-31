# EspoCRM 10.0.9: анализ основы и переход VoiceCRM

Дата анализа: 13 октября 2026. Пути ниже указаны относительно корня проекта
`/Users/ansar.keles/CRM`. Действующий порядок работ и результаты живых проверок
ведутся в [ROADMAP.md](ROADMAP.md) и [HANDOFF.md](HANDOFF.md).

## Решение

EspoCRM становится основой интерфейса и постоянных CRM-данных: контактов,
обращений, звонков, задач, сотрудников и утверждённых знаний. Существующий
FastAPI остаётся отдельным сервисом голосового разговора и проверяемых действий.
Расширение Espo добавляет необходимые поля и элементы интерфейса через штатные
точки расширения. Исходные файлы core сохраняются для обновлений поставщика.

Переход даёт готовую CRM, роли, работу с записями, календарь и настройку сущностей.
Голосовой AI, телефонные каналы, записи всех участков звонка и измерение отклика
до звука требуют собственной реализации и проверки.

## Что именно проверено

Локальная папка `espocrm/` содержит официальный собранный релиз из Docker image
`espocrm/espocrm:10.0.9`. Происхождение описано в
[espocrm/SOURCE.json](../espocrm/SOURCE.json). Версия дополнительно подтверждена
`espocrm/application/Espo/Resources/defaults/config.php`, поле `version`.
Это снимок файлов релиза; Git commit в нём отсутствует.

Проведены полный инвентарь файлов, синтаксический разбор всех JSON, затем
детальный архитектурный разбор Application/DI, маршрутизации и аутентификации,
CRUD/ACL/ORM, ключевых CRM-сущностей, метаданных, jobs/webhooks, вложений,
механизма расширений и основных frontend bundles.

| Проверяемая часть | Результат |
|---|---|
| Файлы снимка | 7 780 |
| PHP | 2 740 файлов |
| JSON | 4 315 файлов; все успешно разобраны стандартным JSON-парсером |
| JavaScript | 40 файлов, включая собранные библиотеки и читаемые `client/lib/original/` |
| Шаблоны | 581 файл `.tpl` |
| Source maps | 21 файл |
| Метаданные `entityDefs` | 70 core-файлов и 25 файлов модуля Crm; часть является наложениями на core-сущности |
| Внутренние модули снимка | Только `Crm` |
| Русские переводы | Есть в core и в Crm: `Resources/i18n/ru_RU/` |

Из снимка исключены `data/`, `vendor/` и рабочие секреты. Отсутствуют manifests
и lockfiles Composer/npm, upstream-тесты и оригинальное дерево разработки
frontend. Карты не содержат `sourcesContent`; например, `client/lib/espo.js.map`
ссылается на `client/src/`, отсутствующий в снимке. Сборка из Git-исходников,
полный аудит сторонних зависимостей и запуск тестов самого Espo требуют
дополнительного точного source checkout/архива релиза.

Разбор JSON подтверждает целостность формата. Он не подтверждает семантику
каждой настройки или отсутствие ошибок во всех 7 780 файлах. Этот отчёт
фиксирует архитектуру и существенные границы интеграции; живые проверки
конкретного экземпляра фиксируются отдельно.

## Технологическая основа

Backend написан на PHP. Локальный
`espocrm/application/Espo/Resources/defaults/systemConfig.php` задаёт
`requiredPhpVersion = 8.3.0`. API использует Slim и PSR HTTP interfaces;
данные обслуживает собственный ORM Espo. Frontend — SPA с собственными views,
моделями, коллекциями, DI и шаблонами. Это видно в `client/lib/original/espo-main.js`
и `client/lib/original/espo-crm.js`.

Официальный repository описывает MySQL/MariaDB/PostgreSQL и отдельную frontend
сборку. Требования нужно сверять для закреплённого релиза, поскольку ветка
`stable` изменяется. [Архитектура upstream](https://github.com/espocrm/espocrm),
[официальная конфигурация сервера](https://docs.espocrm.com/administration/server-configuration/).

Наш `compose.yaml` закрепляет EspoCRM 10.0.9 и MariaDB 11.4. Выделены четыре
сервиса: БД, web app, daemon и WebSocket. Постоянные тома обслуживают БД,
`data`, backend customizations и frontend customizations. HTTP/WS порты
публикуются только на `127.0.0.1:8080` и `127.0.0.1:8083`. Пароли передаются
через файловые secrets; значения в отчёт не включаются.

Источник данных внутри Espo — его собственная БД. FastAPI обращается к Espo
через API. Отдельные оперативные данные голосового сервиса и будущий outbox
имеют собственный жизненный цикл; они не заменяют CRM-записи.

## Карта основных подсистем

| Подсистема | Точные файлы | Назначение и вывод |
|---|---|---|
| Запуск | `espocrm/bootstrap.php`, `espocrm/application/Espo/Core/Application.php` | Bootstrap загружает Composer autoload из runtime; Application создаёт контейнер и запускает выбранный runner |
| DI | `espocrm/application/Espo/Core/Container/ContainerBuilder.php`, `espocrm/application/Espo/Core/InjectableFactory.php` | Сервисы создаются через контейнер и bindings; параметры конструктора определяют зависимости |
| API | `espocrm/application/Espo/Core/Api/Starter.php`, `espocrm/application/Espo/Core/Api/RouteProcessor.php` | Slim routes, middleware, аутентификация и dispatch action/controller |
| CRUD | `espocrm/application/Espo/Core/Controllers/RecordBase.php`, `espocrm/application/Espo/Core/Record/Service.php` | Общая обработка записей: права, фильтры, defaults, валидация, связи, сохранение и ответ |
| ORM | `espocrm/application/Espo/ORM/EntityManager.php`, `espocrm/application/Espo/ORM/Repository/RDBRepository.php` | Query builder, mapper, repositories, транзакции и блокировки |
| CRM hooks | `espocrm/application/Espo/Core/Repositories/Database.php`, `espocrm/application/Espo/Core/HookManager.php` | Идентификаторы, created/modified fields и ORM hooks, включая события после транзакции |
| Права | `espocrm/application/Espo/Core/Acl.php`, `espocrm/application/Espo/Core/Acl/DefaultAccessChecker.php` | Проверка scope/record/ownership: свои записи, команды и совместный доступ |
| Metadata | `espocrm/application/Espo/Core/Utils/Metadata/Builder.php`, `espocrm/application/Espo/Core/Utils/Resource/Reader.php` | Сборка ресурсов core/modules/custom с правилами append и кэшированием |
| Modules | `espocrm/application/Espo/Core/Utils/Module.php`, `espocrm/application/Espo/Core/Utils/ClassFinder.php` | Обнаружение модулей, порядок загрузки и поиск пользовательских реализаций |
| Entity Manager | `espocrm/application/Espo/Tools/EntityManager/EntityManager.php` | Создание и настройка сущностей, полей, связей и переводов в custom-слое |
| Jobs | `espocrm/application/Espo/Core/Job/QueueProcessor.php`, `espocrm/application/Espo/Core/Job/JobRunner.php` | Выбор, запуск и статусы фоновых задач |
| Webhooks | `espocrm/application/Espo/Core/Webhook/Manager.php`, `espocrm/application/Espo/Core/Webhook/Queue.php`, `espocrm/application/Espo/Core/Webhook/Sender.php` | Накопление событий, batch delivery, подпись и ограниченные повторы |
| Вложения | `espocrm/application/Espo/Tools/Attachment/AccessChecker.php`, `espocrm/application/Espo/Tools/Attachment/UploadService.php`, `espocrm/application/Espo/Tools/Attachment/Checker.php` | Допустимые поля/роли, права загрузки, размеры и типы файлов |
| Расширения | `espocrm/application/Espo/Core/Upgrades/Actions/Extension/Install.php`, `espocrm/application/Espo/Core/Upgrades/Actions/Base/Install.php` | Manifest, scripts, копирование файлов, backup и rebuild |
| Frontend | `espocrm/client/lib/original/espo-main.js`, `espocrm/client/lib/original/espo-crm.js`, `espocrm/client/res/templates/` | Карточки, списки, поля, календарь, уведомления и шаблоны |

### Поток обработки операции

```text
HTTP /api/v1
  → Slim route + middleware
  → authentication → текущий пользователь
  → Record controller/service
  → scope ACL → фильтрация и defaults → validation
  → record/assignment/link ACL → duplicate check → hooks
  → ORM repository → БД
  → подготовка output → ответ с подтверждённым record id
```

В `Core/Record/Service.php` метод `create` проверяет права до записи,
обрабатывает validation, assignment и links, а затем вызывает ORM с признаком
API-операции. `update` дополнительно проверяет конкретную существующую запись
и может учитывать version number. Чтение и списки также проходят ACL.

При использовании голого ORM из custom PHP права API-service автоматически
не возникают. Поэтому внешнему голосовому сервису нужен REST-адаптер, а
пользовательским API actions — явные проверки прав и проверенные сервисы.
Прямой SQL обходил бы этот путь и правила обновления данных.

## Сущности для VoiceCRM

Файлы таблицы находятся в
`espocrm/application/Espo/Modules/Crm/Resources/metadata/entityDefs/`.

| Файл | Что хранит | Существенные поля и границы |
|---|---|---|
| `Contact.json` | Известного клиента/контакт | `lastName` required; телефон/email отмечены как персональные данные; связи с Calls, Meetings, Tasks |
| `Lead.json` | Новое обращение/потенциального клиента | Статусы `New`, `Assigned`, `In Process`, `Converted`, `Recycled`, `Dead`; источник `Call`; CRM-конверсия имеет отдельный смысл |
| `Account.json` | Организацию/контрагента | `name` required; контакты и активности связаны с организацией |
| `Call.json` | CRM-активность звонка | `Planned`, `Held`, `Not Held`; `Inbound`/`Outbound`; время, ответственный, участники и parent |
| `Task.json` | Последующее действие сотрудника | `name`, `assignedUser` required; статусы `Not Started`, `Started`, `Completed`, `Canceled`, `Deferred` |
| `Meeting.json` | Событие календаря | Начало/конец, ответственный и участники; хранение события требует отдельного сценария бронирования ресурсов |
| `KnowledgeBaseArticle.json` | Утверждённый материал | `Draft`, `In Review`, `Published`, `Archived`; `body`, read-only `bodyPlain`, publish/expiration dates и категории |
| `Opportunity.json` | Коммерческую возможность | Имя, сумма и close date; создавать только при соответствующем бизнес-сценарии |

Сотрудники, команды и права описаны в core:
`espocrm/application/Espo/Resources/metadata/entityDefs/User.json`, `Team.json`,
`Role.json`. WorkingTimeCalendar предоставляет рабочий календарь; телефонные
внутренние номера и правила перевода требуют собственного справочника.

### Call и телефонный разговор

`Call` — постоянная CRM-запись. Сам факт её создания не начинает телефонный
звонок, не переводит его и не подключает микрофон.
В `Call.json` обязательными помечены имя, начало, конец и ответственный;
для `dateEnd` есть `suppressValidationList: ["required"]`. Адаптеру следует
передавать явно подтверждённые даты, ответственного и направление.
Завершённый состоявшийся разговор можно пометить `Held` после его завершения.

Встроенный `uid` read-only и имеет обычный индекс, а не unique constraint.
Он не обеспечивает дедупликацию внешних voice-событий. Наш `call_id` должен
сохраняться отдельно от Espo record id и от идентификаторов SIP/WebRTC-сессии.

### Контакты и номера

Espo хранит номера в отдельных PhoneNumber-записях и связях. Repository
`espocrm/application/Espo/Repositories/PhoneNumber.php` сохраняет цифровое
представление `numeric`, сохраняя исходное форматирование номера. Поиск должен
учитывать дополнительные номера, а несколько найденных контактов требуют
явной обработки неоднозначности.

В `espocrm/application/Espo/Tools/PhoneNumber/EntityLookup.php:91` обнаружен
локальный нюанс: `findOne($number, $order)` вызывает lookup с `$order`, но
игнорирует возвращённый результат и затем выполняет lookup без этого порядка.
Приоритет Contact/Lead нельзя подтверждать, опираясь на этот helper.
Helper использует ORM; проверка доступа в вызывающем custom action нужна отдельно.
Core в рамках перехода не исправляется: наш путь — ограниченный REST-запрос.

### Знания и запись на приём

`KnowledgeBaseArticle` уже есть в core. Hook
`espocrm/application/Espo/Modules/Crm/Hooks/KnowledgeBaseArticle/SetBodyPlain.php`
подготавливает plain text. Job
`espocrm/application/Espo/Modules/Crm/Jobs/ControlKnowledgeBaseArticleStatus.php`
архивирует истёкшие статьи. Голосовой сервис должен дополнительно проверять
статус и актуальность при загрузке знаний, чтобы задержка фоновой задачи
не делала истёкший прайс рабочим.

Для рабочего помощника нужны утверждённые услуги, цены, адрес, часы и правила.
Хранение статьи про запись и создание Meeting не подтверждают медицинскую
бронь. Требуются расписание ресурсов, обработка конфликтов и однозначный
результат операции. [Официальная база знаний](https://docs.espocrm.com/user-guide/knowledge-base/).

## API и права голосового сервиса

Официальный API имеет корень `api/v1/`, JSON-запросы и CRUD сущностей.
Отдельный API User получает ограниченную роль; сервер использует `X-Api-Key`.
Ключ не передаётся frontend и модели. Аутентификация подтверждена кодом
`espocrm/application/Espo/Core/Authentication/Logins/ApiKey.php`.
[API overview](https://docs.espocrm.com/development/api/),
[CRUD operations](https://docs.espocrm.com/development/api/crud/).

Разрешения задаются для scope, записи, полей и назначения ответственного.
`Role.json` хранит `data` и `fieldData` как JSON-объекты.
`assignmentPermission`/`userPermission` принимают `not-set`, `all`, `team`, `no`;
`exportPermission`/`massUpdatePermission` — `not-set`, `yes`, `no`.
Некорректные значения или JSON-массив вместо объекта нарушают контракт роли.
Фактическое разрешение проверяется запросами от API User, включая отказ в доступе.

Операции нужно выдавать голосовому сервису по необходимости: поиск клиента,
создание Call/Task, позже подтверждённое создание обращения или бронирование.
Удаление, массовые изменения, администрирование и доступ ко всем сущностям
не нужны первому голосовому сценарию.

Локальный `espocrm/application/Espo/Tools/OpenApi/Api/GetSpec.php` проверяет
scope `OpenApi`. Specification доступен через `/api/v1/OpenApi` и включает
настройки экземпляра. Для интеграции это полезнее предположений о стандартном
наборе полей после установки customizations.

Даты API задаются в UTC в формате `YYYY-MM-DD HH:mm:ss`; пользовательское
время клиники нужно переводить с известным часовым поясом. Название «завтра»
не должно превращаться в дату без контекста рабочего календаря.

## Надёжность записи и событий

Успешный POST с record id подтверждает создание CRM-записи. Тайм-аут, обрыв
соединения или некорректный ответ оставляют результат неопределённым:
запись могла сохраниться до потери ответа. Помощник не сообщает успех в таком
случае, а код не повторяет POST вслепую.

Стандартная проверка дублей Espo и ошибка 409 решают собственные задачи CRM.
Они не гарантируют «одно событие звонка — одна запись» при повторной доставке.
Отдельные GET, затем POST подвержены гонкам.

Предлагаемая схема для следующего этапа:

1. Стабильные `call_id` и `event_id` создаёт доверенный сервер.
2. Событие сначала сохраняется в постоянный outbox.
3. В Espo сохраняется внешний идентификатор с unique constraint либо
   специализированный атомарный endpoint расширения.
4. Неопределённый результат сверяется по внешнему идентификатору.
5. Повтор возвращает существующий результат; конфликт содержимого обрабатывается явно.

Транзакции и locks существуют в ORM, но находятся внутри операции Espo.
Несколько HTTP-запросов из Python не становятся одной транзакцией автоматически.

## Jobs, Webhooks и WebSocket

Espo имеет собственные DB-backed Job/ScheduledJob и daemon/cron entrypoints:
`espocrm/daemon.php`, `espocrm/cron.php`, `espocrm/application/Espo/Core/Job/`.
QueueProcessor учитывает pending/running состояния и блокировки;
JobRunner сохраняет успешный/неуспешный результат. Это инфраструктура фоновых
задач CRM, на основе которой можно реализовать нужные jobs расширения.
Аудио и тяжёлый AI-анализ следует вынести из синхронного пути разговора.

Webhooks используют записи WebhookEventQueueItem/WebhookQueueItem. `Sender.php`
отправляет JSON-массив, заголовок `Signature` и пока legacy `X-Signature`.
Современная подпись — base64 от `webhookId:hexHmacSha256(rawPayload, secretKey)`.
Receiver сравнивает её безопасным способом, сохраняет событие и учитывает повторы.

**Расхождение документации и релиза:** официальная страница описывает отправку
по умолчанию раз в 5 минут. В проверенном 10.0.9
`espocrm/application/Espo/Resources/metadata/app/scheduledJobs.json` содержит
`ProcessWebhookQueue.scheduling = "*/2 * * * *"` — раз в 2 минуты.
Действующий экземпляр может иметь изменённое расписание в БД. Для нашего
сценария существенен сам факт очереди: Webhook не подтверждает действие в
голосовом разговоре за одну секунду. [Документация Webhooks](https://docs.espocrm.com/administration/webhooks/).

`Core/Webhook/Queue.php` задаёт по умолчанию 4 попытки и период 10 минут
между неудачными попытками. Это ограниченная доставка, требующая контроля
неуспешных событий. `Sender.php` ограничивает внутренние адреса и перенаправления;
для конкретного локального receiver нужен явный `webhookAllowedAddressList`
в формате `host:port`. Проверку внутренних адресов целиком не отключаем.

Штатный WebSocket используется для уведомлений, обновления карточек и календаря;
это видно по topics `recordUpdate`, `calendarUpdate`, `newNotification` в
frontend bundles. Он не является транспортом голосового аудио. Голос
продолжает использовать WebRTC/OpenAI или будущий media bridge Asterisk.

## Расширение без изменения core

Пользовательские реализации ищутся в custom/modules/core; порядок модулей
управляется `Resources/module.json`. Настройки карточек, полей, layouts и
обработчиков уже разделены по metadata. Для Call это показано в:

- `espocrm/application/Espo/Modules/Crm/Resources/metadata/entityDefs/Call.json`;
- `espocrm/application/Espo/Modules/Crm/Resources/metadata/clientDefs/Call.json`;
- `espocrm/application/Espo/Modules/Crm/Resources/metadata/recordDefs/Call.json`.

Предлагаемое расположение нашего будущего расширения внутри runtime:

```text
custom/Espo/Modules/VoiceCrm/
  Resources/module.json
  Resources/routes.json
  Resources/metadata/entityDefs/Call.json
  Resources/metadata/clientDefs/Call.json
  Resources/metadata/recordDefs/Call.json
  Hooks/...
  Api/...
client/custom/modules/voice-crm/
  src/...
  res/templates/...
```

Это схема следующего слоя разработки, а не перечисление уже установленного
пакета. Названия custom-полей нужно закрепить вместе с API-контрактом.
Примеры кандидатов: внешний идентификатор звонка/события, результат разговора,
расшифровка, идентификатор записи и ссылка на защищённое воспроизведение.
Объёмные аудиофайлы не записываются в текстовое поле.

В `espocrm/application/Espo/Services/Record.php` прямо рекомендуется composition
через `recordDefs`, а не расширение общего Record service. Repository также
рекомендует hooks вместо override устаревающих методов.
`lateAfterSave`/`lateAfterRemove` в 10.x позволяют реагировать после commit;
медленные внешние вызовы всё равно следует отправлять в очередь.
[Modules](https://docs.espocrm.com/development/modules/),
[Hooks](https://docs.espocrm.com/development/hooks/).

Extension package содержит `manifest.json`, `files/`, `scripts/`; поддерживает
проверку версии, установочные скрипты и rebuild. Свои пакеты должны записывать
custom-слой. Собранные `client/lib/espo*.js` не используются для ручных правок.
Entity Manager тоже сохраняет изменения в custom и добавляет префикс `c`
для создаваемых через UI полей/сущностей. После изменения метаданных нужны
соответствующие cache/rebuild действия.
[Extension packages](https://docs.espocrm.com/development/extension-packages/),
[Entity Manager](https://docs.espocrm.com/administration/entity-manager/).

## Core и отдельные дополнения

В локальном снимке присутствует только модуль Crm. Платные и дополнительные
пакеты автоматически в него не входят.

| Возможность | Где доступна | Значение для проекта |
|---|---|---|
| Contact/Lead/Call/Task/Meeting, роли, API, знания, Entity Manager | Core | Достаточно для первого подключения голосового сервиса |
| Reports/BPM/Workflows | Advanced Pack | Отдельный коммерческий пакет; не нужен для первого REST-адаптера |
| Products/prices/quotes/orders/invoices | Sales Pack | Отдельный коммерческий пакет; бизнес-модель компании всё равно нужно настроить |
| Asterisk/3CX/Twilio и другие PBX integrations | VoIP Integration | Отдельный коммерческий пакет; не предоставляет наш голосовой AI |
| Meeting Scheduler | Отдельное расширение | Отличается от core Calendar и планирования существующих активностей |
| MCP endpoints | Отдельное бесплатное AGPL-расширение MCP | Возможный дополнительный интерфейс; первый адаптер использует REST |

Описание пакетов проверено на официальных страницах; текущие цены не
фиксировались. [Advanced Pack](https://www.espocrm.com/extensions/advanced-pack/),
[Sales Pack](https://www.espocrm.com/extensions/sales-pack/),
[VoIP Integration](https://www.espocrm.com/extensions/voip-integration/),
[MCP extension](https://www.espocrm.com/extensions/mcp/).

Официальный Asterisk-коннектор VoIP работает через AMI: события и click-to-call.
Запись настраивается на Asterisk, а URL сохраняется в CRM. Для AI-разговора
нужны отдельные media/ARI-интеграция, аудиоформаты, VAD, перебивания и call state.
Покупка VoIP-пакета не закрывает эти задачи.
[Asterisk integration](https://docs.espocrm.com/extensions/voip-integration/asterisk-integration-setup/),
[работа с записями](https://docs.espocrm.com/extensions/voip-integration/customization/).

## Безопасность и эксплуатация

В core есть аутентификация API Key/HMAC, login hooks, 2FA/OIDC implementations,
scope/record/field ACL, фильтрация CRUD и проверка вложений. Наличие механизмов
не подтверждает их настройку на рабочем экземпляре. Проверяется конкретная
API-роль и реальные разрешённые/запрещённые действия.

`espocrm/.htaccess` запрещает прямой доступ к `data`, `application`, `custom`
и `vendor`, отключает directory indexing и направляет web-запросы в `public`.
При другом web server нужны соответствующие правила; локальный source snapshot
сам по себе не является document root для публикации.

Для записей разговора используются проверяемые права и защищённая выдача
через attachment/storage layer или собственный audio gateway. Простое поле
URL не делает аудио закрытым. Для будущих записей нужно согласовать срок
хранения, удаление и доступ сотрудников; все участки после перевода сохраняют
общую привязку к звонку.

Экспортируемые тексты из CRM трактуются как данные, а не инструкции для агента.
Модель не выбирает произвольный endpoint, SQL, SIP-адрес или роль. Сервер
проверяет аргументы инструмента, права, допустимое назначение и результат.

Постоянные тома требуется резервировать вместе: БД, data и customizations.
Проверка восстановления важнее наличия файла backup. Обновление core и
проверка extension выполняются на отдельном экземпляре с сохранением версии.

## Сопоставление с текущим VoiceCRM

В момент анализа существующий код уже выделяет EspoCRM как провайдера:

| Файл | Роль в переходе |
|---|---|
| `compose.yaml` | Закреплённый локальный Espo runtime, MariaDB, daemon, WebSocket и постоянные тома |
| `src/voicecrm/main.py` | Главный адрес перенаправляет к CRM; прежний файловый голосовой стенд доступен на `/voice`, Realtime на `/realtime` |
| `src/voicecrm/config.py` | Настройки URL/тайм-аута и серверного ключа Espo |
| `src/voicecrm/crm/client.py` | Ограниченный REST-адаптер: доступность, поиск контактов, внутренние методы создания Call/Task и фиксированные ошибки |
| `src/voicecrm/crm/router.py` | Локальная read-only диагностика `/crm/status` и `/crm/contacts`; публичные операции создания записей не открыты |
| `src/voicecrm/dialogue/` | OpenAI, Alem STT, Realtime и диалоговый контекст остаются голосовым сервисом |
| `src/voicecrm/telephony/` | SIP/Asterisk/media bridge требует реализации |
| `src/voicecrm/analytics/` | Записи и AI-анализ требуют реализации |

Адаптер сохраняет неопределённый результат записи как `write_unconfirmed`
и не делает автоматический retry POST. Он проверяет ответ и receipt id,
переводит даты в UTC и не следует HTTP redirects с API-ключом.
Гарантированный outbox/idempotency и голосовые tool calls остаются следующим слоем.
Конфигурация и наличие этих методов не заменяют живую приёмку от API User.

Основной файловый путь теперь `/voice`: запись → Alem STT → текстовый OpenAI
→ браузерный SpeechSynthesis. Он остаётся последовательным прототипом.
Realtime обрабатывает потоковый голос напрямую; CRM подключается через
серверные инструменты, когда их контракт и подтверждение будут реализованы.

## План перехода и критерии закрытия

| Этап | Реализация | Проверяемый результат |
|---|---|---|
| 1. CRM-основа | Закрепить runtime, русский интерфейс, постоянные тома и администратора | Вход в Espo; данные сохраняются после рестарта; чужие процессы не затронуты |
| 2. Доступ сервиса | API User, ограниченная роль, конфигурация и REST-адаптер | Реальный read и целевые create работают; запрещённые операции отклоняются |
| 3. Голос в интерфейсе | Пользовательский пункт/панель/маршрут голоса, Call-карточка | Пользователь начинает и завершает тест; состояния и итог отображаются однозначно |
| 4. Подтверждаемые действия | Серверные tool calls: поиск клиента, итог Call, задача/обращение | Помощник сообщает результат после API receipt; ошибка не превращается в успех |
| 5. Повторы | Постоянный outbox, внешние ids, атомарная дедупликация | Повтор/тайм-аут/рестарт не создаёт вторую запись одного события |
| 6. Знания | Утверждённые Published-материалы и краткий актуальный контекст | Проверяемые цены/условия; неизвестные сведения не выдумываются |
| 7. Телефония | Linux Asterisk, SIP, call state, media bridge, справочник и fallback | Робот отвечает в реальном звонке и переводит только на разрешённого сотрудника |
| 8. Записи и анализ | Все участки, закрытое хранение, фоновые STT/AI jobs | Запись включает участок сотрудника; анализ содержит доказательства и таймкоды |
| 9. Пилот | Нагрузка, отказы, восстановление, доступы и метрики | Выполняются согласованные показатели на реальных звонках |

Это критерии всего перехода. Статус отдельных шагов и выполненные live tests
обновляются в HANDOFF/ROADMAP; наличие готового core не закрывает последующие шаги.

## Цель около одной секунды

Смена CRM даёт постоянные данные и интерфейс. Она не ускоряет файловый STT.
Для быстрого первого звука сохраняются потоковый Realtime, постоянное
соединение, короткие ответы, настройка конца речи и отмена при перебивании.
Краткие утверждённые знания можно загрузить до звонка; тяжёлый анализ и
логирование выносятся из пути первой голосовой реплики.

Отдельно измеряются:

1. Конец речи → первый слышимый звук: полный пользовательский показатель.
2. Получение `speech_stopped` → получение `output_audio_buffer.started`:
   отклик сервиса, исключающий VAD-паузу и физическое воспроизведение.
3. Запрос инструмента → подтверждённый результат CRM: время внешнего действия.

Цель по первому показателю — около 1 секунды на простых репликах. Требуются
живые новые вопросы, границы измерения, размер выборки, p50/p95, доля ответов
дольше секунды, ошибки и перебивания. Unit-тесты, HTTP 200 и короткая фраза
начала обработки не доказывают ни голосовую секунду, ни завершённую запись.

## Лицензия

`espocrm/LICENSE.txt` содержит GNU AGPLv3. Заголовки исходных PHP-файлов
разрешают AGPLv3 или более позднюю версию. В
`espocrm/application/Espo/Core/Application.php:21` закреплено сохранение
Appropriate Legal Notices; далее по Section 7(b) требуется отображение слова
«EspoCRM». Эти notices сохраняются при изменении интерфейса.

Section 13 описывает предоставление исходников модифицированной версии
пользователям, взаимодействующим с ней по сети. Размещение изменений в extension
и разделение сервисов выбраны для сопровождения архитектуры; они не являются
автоматическим исключением из лицензии. Снимок собранного релиза с исключёнными
частями используется для анализа; исходники модификаций и соответствующий
набор исходников сборки должны сохраняться отдельно.
[Официальная лицензия EspoCRM](https://www.espocrm.com/espocrm-open-source-license/).

## Существенные выводы для следующей работы

- Основа CRM готова у Espo; разрабатывать свою CRUD-CRM с нуля не требуется.
- Первый серверный адаптер обходится core без покупки Advanced/Sales/VoIP.
- Call-запись, телефонный канал и медицинская бронь имеют разные подтверждения.
- Метаданные и hooks позволяют добавлять голосовые поля и UI с сохранением core.
- События webhook обрабатываются в фоне; действие в разговоре подтверждает REST-ответ.
- Постоянная доставка и дедупликация требуют отдельной реализации.
- Браузерный WebSocket Espo обновляет CRM; голос продолжает использовать свой транспорт.
- Срок в одну секунду и готовность реальных действий подтверждаются отдельными live tests.
