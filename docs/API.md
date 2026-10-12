# Проверенные официальные API

Документация изучена 12.10.2026. В проекте используются перечисленные ниже методы, без недокументированных API.

## Telegram

- [Bot API](https://core.telegram.org/bots/api): `getMe`, `getWebhookInfo`, `setWebhook`, `sendMessage`, `answerCallbackQuery`; webhook с `X-Telegram-Bot-Api-Secret-Token`.
- [KeyboardButton](https://core.telegram.org/bots/api#keyboardbutton): `request_contact=true` в ReplyKeyboardMarkup; Contact.user_id сравнивается с message.from.id.
- [Deep linking](https://core.telegram.org/bots/features#deep-linking): `https://t.me/<username>?start=<token>`, параметр до 64 символов. Используется URL-safe случайный токен длиной 43 символа.

## MAX

- [API](https://dev.max.ru/docs-api): домен `platform-api2.max.ru`, токен в Authorization, проверка TLS.
- [POST /messages](https://dev.max.ru/docs-api/methods/POST/messages): `user_id` query, текст и attachments в JSON.
- [POST /answers](https://dev.max.ru/docs-api/methods/POST/answers): callback_id query.
- [POST /subscriptions](https://dev.max.ru/docs-api/methods/POST/subscriptions): HTTPS URL, update_types, secret; `X-Max-Bot-Api-Secret`.
- [Клавиатуры и request_contact](https://dev.max.ru/docs-api/use-cases/sending-messages/keyboard): inline_keyboard с payload.buttons; проверка HMAC-SHA256(bot_token, vcf_info).
- [Deep links](https://dev.max.ru/help/deeplinks): `https://max.ru/<botName>?start=<payload>`; bot_started.payload.
- [Официальная OpenAPI-схема](https://github.com/max-messenger/api-schema/blob/main/schema.yaml): проверены MessageCreatedUpdate, MessageCallbackUpdate, BotStartedUpdate, BotStoppedUpdate, ContactAttachmentPayload, Recipient. Схема nullable полей учтена; актуальный host взят из веб-документации.

## amoCRM

- [Контакты](https://www.amocrm.ru/developers/content/crm_platform/contacts-api): `GET /api/v4/contacts?query=...`, `GET /api/v4/contacts/{id}?with=leads`, custom_fields_values / PHONE.
- [Сделки](https://www.amocrm.ru/developers/content/crm_platform/leads-api): `GET /api/v4/leads/{id}?with=contacts`, текущие pipeline_id/status_id.
- [Воронки](https://www.amocrm.ru/developers/content/crm_platform/leads_pipelines): `GET /api/v4/leads/pipelines`, `GET /api/v4/leads/pipelines/{id}`. Реальный аккаунт подтвердил 8993570, «Успешно завершенные».
- [Примечания](https://www.amocrm.ru/developers/content/crm_platform/events-and-notes): `GET /api/v4/leads/{id}/notes`, `POST /api/v4/leads/{id}/notes`, массив объектов с note_type common и params.text. request_id не используется как гарантия идемпотентности.
- [Webhook API](https://www.amocrm.ru/developers/content/crm_platform/webhooks-api) и [формат](https://www.amocrm.ru/developers/content/crm_platform/webhooks-format): status_lead / update_lead; form-поля leads[status][N][id], leads[update][N][id]. Официальный HMAC-заголовок не придуман: используется секретный endpoint и контроль через GET карточки.
- [OAuth2](https://www.amocrm.ru/developers/content/oauth/step-by-step): `POST /oauth2/access_token` с grant_type refresh_token. Долгосрочный токен не требует refresh; обычный — требует credentials интеграции.

Права существующего amoCRM токена проверены только чтением воронок. Создание примечаний намеренно не выполнялось при разработке.
