import json
from unittest.mock import AsyncMock,patch
import httpx
import pytest
from sinai.content import capture
from sinai.http import HTTP,RetryLater


@pytest.mark.parametrize('kind',['image','video','file','audio'])
async def test_telegram_media_upload_to_max_and_token_reuse_shape(system,tmp_path,kind):
    path = tmp_path/'asset'
    path.write_bytes(b'fake media')
    calls = []
    def handler(request):
        calls.append(request)
        if request.url.path == '/uploads':
            assert request.url.params['type'] == kind
            return httpx.Response(200,json={'url':'https://upload.example/upload','token':'generic-token'})
        if request.url.path == '/upload':
            assert b'fake media' in request.content
            return httpx.Response(200,json={'photos':{'123':{'token':'image-token'}}} if kind == 'image' else {'token':'generic-token'})
        return httpx.Response(200,json={'message':{'body':{'mid':'sent-mid'}}})
    bot = system.platforms['max']
    await bot.http.close()
    bot.http = HTTP('max',client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),interval=0)
    content = system.service.campaigns.content
    content.download = AsyncMock(return_value=path)
    source = {'platform':'telegram','text':'Подпись','html':'<b>Подпись</b>','native_only':False,'media':[{'kind':kind,'file_id':'f','name':'asset.bin'}]}
    with patch('sinai.content.public_url',new_callable=AsyncMock):
        prepared = await content.prepare(source,'max')
    system.config.test_mode = False
    await content.send(prepared,'max',70,70)
    body = json.loads(calls[-1].content)
    assert body['format'] == 'html' and body['text'] == '<b>Подпись</b>'
    assert body['attachments'][0]['type'] == kind and body['attachments'][0]['payload']['token']
    assert len(calls) == 3


@pytest.mark.parametrize('kind,method',[('image','sendPhoto'),('video','sendVideo'),('file','sendDocument'),('audio','sendAudio')])
async def test_max_media_to_telegram_upload_methods(system,tmp_path,kind,method):
    path = tmp_path/'asset'
    path.write_bytes(b'fake max file')
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200,json={'ok':True,'result':{'message_id':900}})
    bot = system.platforms['telegram']
    await bot.http.close()
    bot.http = HTTP('telegram',client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),interval=0)
    content = system.service.campaigns.content
    content.download = AsyncMock(return_value=path)
    source = {'platform':'max','text':'Подпись','html':'<i>Подпись</i>','native_only':False,'media':[{'kind':kind,'url':'https://cdn.example/file','name':'asset.bin'}]}
    prepared = await content.prepare(source,'telegram')
    system.config.test_mode = False
    await content.send(prepared,'telegram',50,50)
    assert calls[0].url.path.endswith('/'+method)
    assert b'fake max file' in calls[0].content and b'HTML' in calls[0].content


async def test_attachment_not_ready_is_retryable_known_rejection(system):
    bot = system.platforms['max']
    await bot.http.close()
    bot.http = HTTP('max',client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(400,json={'code':'attachment.not.ready','message':'not processed'}))),interval=0)
    with pytest.raises(RetryLater):
        await bot.call('POST','/messages',json={'attachments':[]})


async def test_native_max_uses_documented_forward_link(system):
    bot = system.platforms['max']
    await bot.http.close()
    seen = []
    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200,json={'message':{'body':{'mid':'forwarded'}}})
    bot.http = HTTP('max',client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),interval=0)
    source = capture('max',{'body':{'mid':'original','text':'text','attachments':[{'type':'file','payload':{'token':'f'}}]}})
    payload = await system.service.campaigns.content.prepare(source,'max')
    system.config.test_mode = False
    await system.service.campaigns.content.send(payload,'max',70,70)
    assert seen[0] == {'link':{'type':'forward','mid':'original'}}


async def test_native_telegram_album_uses_copy_messages(system):
    bot = system.platforms['telegram']
    await bot.http.close()
    seen = []
    def handler(request):
        seen.append(json.loads(request.content))
        assert request.url.path.endswith('/copyMessages')
        return httpx.Response(200,json={'ok':True,'result':[{'message_id':900},{'message_id':901}]})
    bot.http = HTTP('telegram',client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),interval=0)
    system.config.test_mode = False
    result = await system.service.campaigns.content.send({'method':'copy','source':{'platform':'telegram','chat_id':1,'message_ids':[10,11]}},'telegram',50,50)
    assert result == '900,901' and seen[0]['message_ids'] == [10,11]
