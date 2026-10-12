import re
from urllib.parse import urlparse


def parse_lead(value, base_url):
    if value.isascii() and value.isdigit() and 0 < int(value) < 2**63:
        return int(value)
    url = urlparse(value)
    if url.scheme != 'https' or url.hostname != urlparse(base_url).hostname:
        raise ValueError('Укажите ID или HTTPS-ссылку сделки своего аккаунта amoCRM.')
    match = re.fullmatch(r'/leads/detail/(\d+)/?', url.path)
    if not match or not 0 < int(match[1]) < 2**63:
        raise ValueError('Ожидается ссылка /leads/detail/ID.')
    return int(match[1])
