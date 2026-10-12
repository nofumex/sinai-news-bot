# Официальные API · v2 · проверка 12.10.2026

## Telegram

[Bot API](https://core.telegram.org/bots/api): getMe/getWebhookInfo для диагностики; deleteWebhook(drop_pending_updates=false) — явная подготовка polling; getUpdates с offset, limit, timeout, allowed_updates. Cursor сохраняется вместе с inbox до следующего запроса.

sendMessage, answerCallbackQuery, request_contact в ReplyKeyboardMarkup с contact.user_id; [числовые deep links](https://core.telegram.org/bots/features#deep-linking). Личность не проверяется самим start-параметром.

copyMessage сохраняет сообщение; copyMessages сохраняет альбом. Неподдерживаемые invoice/paid media/giveaways/service messages не принимаются как кампании. Cross-media: getFile, sendPhoto/sendVideo/sendDocument/sendAudio/sendMediaGroup с multipart и HTML. Платные ускоренные рассылки не используются.

## MAX

[Обзор API](https://dev.max.ru/docs-api): platform-api2.max.ru; Authorization содержит bot token, без Bearer; сертификаты проверяются.

[GET /updates](https://dev.max.ru/docs-api/methods/GET/updates): timeout 0–90, limit 1–1000, types через запятую, marker int64. Пропуск marker возвращает последнее событие. MAX рекомендует webhook для production и ограничивает polling скоростью и хранением; численные гарантии не опубликованы на этой странице.

GET /subscriptions и DELETE /subscriptions?url=... — явная подготовка только этого бота к polling. POST /messages принимает user_id query, text/format/attachments/link в JSON; POST /answers — callback_id query. link.type=forward с mid используется для нативных кампаний. Модели MessageBody, MarkupElement, NewMessageLink сверены с [официальной OpenAPI-схемой](https://github.com/max-messenger/api-schema/blob/main/schema.yaml).

[Форматирование](https://dev.max.ru/docs-api/use-cases/sending-messages/text-formatting): HTML/Markdown; [request_contact](https://dev.max.ru/docs-api/use-cases/sending-messages/keyboard) — HMAC-SHA256(bot_token, vcf_info), не произвольный контакт из адресной книги.

[Медиа](https://dev.max.ru/docs-api/use-cases/sending-messages/media) и [POST /uploads](https://dev.max.ru/docs-api/methods/POST/uploads): image/video/audio/file, загрузка multipart поля data на полученный URL, повторно используемый token в attachments.payload.token. image upload возвращает photos с токенами; video/audio могут вернуть token при выдаче upload URL; file возвращает его после upload. Код attachment.not.ready — документированный отказ с возможностью повтора позже. Несовместимые типы/комбинации требуют отдельного нативного варианта.

## amoCRM

[Контакты](https://www.amocrm.ru/developers/content/crm_platform/contacts-api): GET /api/v4/contacts?query=... с пагинацией и PHONE-проверкой; GET /api/v4/contacts/{id}?with=leads. Поиска имён нет.

[Сделки](https://www.amocrm.ru/developers/content/crm_platform/leads-api): GET /api/v4/leads/{id}?with=contacts, актуальные pipeline_id/status_id. [Воронки](https://www.amocrm.ru/developers/content/crm_platform/leads_pipelines): GET /api/v4/leads/pipelines/{id}; проверяется 8993570 и название «Успешно завершенные».

[Примечания](https://www.amocrm.ru/developers/content/crm_platform/events-and-notes): GET/POST /api/v4/leads/{id}/notes, массив common / params.text. request_id не считается ключом идемпотентности. [OAuth2](https://www.amocrm.ru/developers/content/oauth/step-by-step): POST /oauth2/access_token, grant_type=refresh_token.

Периодический опрос заменяет CRM webhook. В коде нет создания CRM-карточек, регистрации webhook amoCRM или недокументированного HMAC CRM.
