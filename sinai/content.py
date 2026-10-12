"""Native copies keep source fidelity; cross-platform content is prepared once before preview."""
import hashlib
import html
import ipaddress
import socket
from pathlib import Path
from urllib.parse import urlparse
import httpx
from .http import APIError, AmbiguousSend

TAGS = {'bold': 'b', 'italic': 'i', 'underline': 'u', 'strikethrough': 's', 'code': 'code',
        'pre': 'pre', 'blockquote': 'blockquote', 'strong': 'b', 'emphasized': 'i',
        'monospaced': 'code', 'quote': 'blockquote'}


def formatted(text, entities, platform):
    text = text or ''
    units = text.encode('utf-16-le')
    spans = []
    for entity in entities or []:
        kind = entity['type']
        start = entity.get('offset', entity.get('from', 0))
        end = start + entity['length']
        if not 0 <= start < end <= len(units)//2:
            continue
        tag = TAGS.get(kind)
        href = entity.get('url')
        if kind == 'url':
            href = units[start*2:end*2].decode('utf-16-le')
        if kind in {'text_link', 'link', 'url'} and href:
            tag = 'a href="'+html.escape(href, quote=True)+'"'
        if tag:
            spans.append((start, end, tag))
    edges = sorted({0, len(units)//2, *(x for s in spans for x in s[:2])})
    result = []
    for left, right in zip(edges, edges[1:]):
        active = sorted((s for s in spans if s[0] <= left and s[1] >= right), key=lambda s: (s[0], -s[1], s[2]))
        result.extend('<'+s[2]+'>' for s in active)
        result.append(html.escape(units[left*2:right*2].decode('utf-16-le')))
        result.extend('</'+s[2].split()[0]+'>' for s in reversed(active))
    return ''.join(result)


def capture(platform, message):
    if platform == 'telegram':
        forbidden = {'invoice','paid_media','giveaway','giveaway_winners'}
        if forbidden & message.keys() or (message.get('poll',{}).get('type') == 'quiz'):
            raise ValueError('Этот тип сообщения нельзя копировать через Bot API.')
        text = message.get('text', message.get('caption', ''))
        media = []
        if message.get('photo'):
            media.append({'kind': 'image', 'file_id': message['photo'][-1]['file_id'], 'name': 'photo.jpg'})
        for key, kind in [('video','video'), ('animation','file'), ('document','file'), ('audio','audio'), ('voice','audio')]:
            if message.get(key):
                item = message[key]
                media.append({'kind': kind, 'file_id': item['file_id'], 'name': item.get('file_name', key+'.bin')})
        if not text and not media and not any(x in message for x in ['sticker','location','contact','poll','video_note']):
            raise ValueError('Отправьте текст, фото, видео, документ или поддерживаемое вложение.')
        return {'platform': platform, 'chat_id': message['chat']['id'], 'message_id': message['message_id'],
                'html': formatted(text, message.get('entities', message.get('caption_entities')), platform),
                'text': text, 'media': media, 'native_only': any(x in message for x in ['sticker','location','contact','poll','video_note']),
                'unsupported_format': any(e['type'] in {'spoiler','custom_emoji','text_mention','expandable_blockquote'} for e in message.get('entities',message.get('caption_entities',[]))),
                'group': message.get('media_group_id')}
    body = message.get('body') or {}
    attachments = body.get('attachments') or []
    text = body.get('text') or ''
    if not text and not attachments:
        raise ValueError('Отправьте сообщение с текстом или вложением.')
    media = []
    for item in attachments:
        kind, data = item['type'], item.get('payload') or {}
        if kind in {'image','video','audio','file'}:
            url = data.get('url')
            if not url and data.get('urls'):
                url = next((x for x in data['urls'].values() if isinstance(x, str)), None)
            media.append({'kind': kind, 'url': url, 'name': data.get('name') or kind+'.bin'})
    return {'platform': platform, 'mid': body['mid'], 'html': formatted(text, body.get('markup'), platform),
            'text': text, 'media': media, 'native_only': any(x['type'] not in {'image','video','audio','file'} for x in attachments),
            'unsupported_format': any(e['type'] in {'highlighted','heading','user_mention'} for e in body.get('markup') or [])}


async def public_url(url):
    parsed = urlparse(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Для медиа требуется публичный HTTPS URL')
    import asyncio
    addresses = await asyncio.to_thread(socket.getaddrinfo, parsed.hostname, parsed.port or 443)
    if not addresses or any(not ipaddress.ip_address(x[4][0]).is_global for x in addresses):
        raise ValueError('Локальные адреса медиа запрещены')


class Content:
    def __init__(self, config, platforms):
        self.config, self.platforms = config, platforms

    async def download(self, source):
        key = hashlib.sha256(str(source).encode()).hexdigest()
        directory = self.config.database_path.parent/'media'
        directory.mkdir(exist_ok=True, mode=0o700)
        path = directory/key
        if path.exists():
            return path
        if source.get('file_id'):
            tg = self.platforms['telegram']
            info = await tg.call('getFile', {'file_id': source['file_id']}, safe=True)
            if info.get('file_size',0) > self.config.media_max_bytes:
                raise ValueError('Файл превышает MEDIA_MAX_BYTES / лимит скачивания Telegram getFile')
            url = 'https://api.telegram.org/file/bot'+self.config.tg_bot_token.get_secret_value()+'/'+info['file_path']
            client = tg.http.client
        else:
            url = source.get('url')
            if not url:
                raise ValueError('Нет URL медиа для переноса. Добавьте отдельный вариант сообщения для другой платформы.')
            client = self.platforms['max'].http.client
        temporary = path.with_suffix('.part')
        try:
            for _ in range(5):
                await public_url(url)
                async with client.stream('GET', url, timeout=60) as response:
                    if response.is_redirect:
                        url = str(response.url.join(response.headers['location']))
                        continue
                    if response.status_code != 200:
                        raise APIError('media', response.status_code)
                    size = 0
                    with temporary.open('wb') as file:
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > self.config.media_max_bytes:
                                raise ValueError('Файл превышает MEDIA_MAX_BYTES')
                            file.write(chunk)
                    temporary.chmod(0o600)
                    temporary.replace(path)
                    return path
            raise ValueError('Слишком много перенаправлений медиа')
        except httpx.TransportError:
            raise APIError('media', 503, 'download_failed') from None
        finally:
            temporary.unlink(missing_ok=True)

    async def prepare(self, source, target):
        if source['platform'] == target:
            return {'method': 'copy', 'source': source}
        if source['native_only'] or source.get('unsupported_format'):
            raise ValueError('Это вложение нельзя перенести без изменений. Добавьте отдельный вариант для '+target+'.')
        limit = 4000 if target == 'max' else (1024 if source['media'] else 4096)
        if len(source['text'].encode('utf-16-le'))//2 > limit:
            raise ValueError('Текст/подпись превышает лимит '+target+'. Добавьте отдельный вариант.')
        assets = []
        kinds = {m['kind'] for m in source['media']}
        if len(source['media']) > (10 if target == 'telegram' else 12):
            raise ValueError('Количество вложений превышает лимит '+target)
        if target == 'telegram' and len(kinds) > 1 and kinds & {'file','audio'}:
            raise ValueError('Telegram требует отдельный вариант смешанного альбома')
        if target == 'max' and len(source['media']) > 1 and kinds & {'file','audio'}:
            raise ValueError('MAX требует отдельный вариант с одним файлом/аудио')
        for media in source['media']:
            path = await self.download(media)
            if target == 'telegram':
                assets.append(dict(media, path=str(path)))
            else:
                bot = self.platforms['max']
                upload = await bot.call('POST', '/uploads', params={'type': media['kind']}, safe=True)
                await public_url(upload['url'])
                with path.open('rb') as file:
                    data = await bot.http.request('POST', upload['url'], safe=True,
                          headers={'Authorization': self.config.max_bot_token.get_secret_value()},
                          files={'data': (Path(media['name']).name, file)}, timeout=120)
                token = data.get('token') or upload.get('token')
                if media['kind'] == 'image':
                    token = next((x.get('token') for x in data.get('photos',{}).values() if x.get('token')), None)
                if not token:
                    raise ValueError('MAX не вернул токен вложения')
                assets.append({'type': media['kind'], 'payload': {'token': token}})
        return {'method': 'converted', 'html': source['html'], 'assets': assets}

    async def send(self, payload, target, user_id, chat_id):
        bot = self.platforms[target]
        if self.config.test_mode:
            return 'test'
        if payload['method'] == 'copy':
            source = payload['source']
            if target == 'telegram':
                if source.get('message_ids'):
                    result = await bot.call('copyMessages',{'chat_id':chat_id,'from_chat_id':source['chat_id'],'message_ids':source['message_ids']})
                    if len(result) != len(source['message_ids']):
                        raise AmbiguousSend('telegram',200,'album_partially_copied')
                    return ','.join(str(x['message_id']) for x in result)
                result = await bot.call('copyMessage', {'chat_id': chat_id, 'from_chat_id': source['chat_id'], 'message_id': source['message_id']})
                return str(result['message_id'])
            result = await bot.call('POST','/messages', params={'user_id': user_id}, json={'link': {'type':'forward','mid':source['mid']}})
            return result['message']['body']['mid']
        if target == 'max':
            body = {'text': payload['html'], 'format': 'html'}
            if payload['assets']:
                body['attachments'] = payload['assets']
            result = await bot.call('POST','/messages', params={'user_id':user_id}, json=body)
            return result['message']['body']['mid']
        assets = payload['assets']
        if not assets:
            result = await bot.call('sendMessage', {'chat_id':chat_id,'text':payload['html'],'parse_mode':'HTML'})
            return str(result['message_id'])
        if len(assets) > 1:
            # A MAX multi-attachment message has no exact TG analogue: preserve all media as one album.
            media, files = [], {}
            import json
            from contextlib import ExitStack
            with ExitStack() as stack:
                for i, asset in enumerate(assets):
                    kind = {'image':'photo','video':'video','file':'document','audio':'audio'}[asset['kind']]
                    files[f'file{i}'] = (Path(asset['name']).name, stack.enter_context(open(asset['path'],'rb')))
                    item = {'type':kind,'media':f'attach://file{i}'}
                    if i == 0:
                        item.update(caption=payload['html'],parse_mode='HTML')
                    media.append(item)
                if len({x['type'] for x in media}) > 1 and any(x['type'] in {'document','audio'} for x in media):
                    raise ValueError('Telegram требует отдельный вариант для смешанных файлов/аудио альбома')
                response = await bot.http.request('POST',bot.base+'sendMediaGroup',safe=False,
                                                  data={'chat_id':str(chat_id),'media':json.dumps(media)},files=files)
                return ','.join(str(x['message_id']) for x in response['result'])
        asset = assets[0]
        field, method = {'image':('photo','sendPhoto'),'video':('video','sendVideo'),
                         'file':('document','sendDocument'),'audio':('audio','sendAudio')}[asset['kind']]
        with open(asset['path'],'rb') as file:
            response = await bot.http.request('POST',bot.base+method,safe=False,
                  data={'chat_id':str(chat_id),'caption':payload['html'],'parse_mode':'HTML'},files={field:(Path(asset['name']).name,file)})
        return str(response['result']['message_id'])
