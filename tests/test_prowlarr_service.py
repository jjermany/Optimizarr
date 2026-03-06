from types import SimpleNamespace

from app.services import prowlarr_service


def test_search_uses_100_result_limit(monkeypatch):
    captured = {}

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return []

    def _fake_get(url, params=None, headers=None, timeout=None):
        captured['url'] = url
        captured['params'] = params
        captured['headers'] = headers
        captured['timeout'] = timeout
        return _Response()

    monkeypatch.setattr(prowlarr_service.secrets_store, 'decrypt_secret', lambda value: value)
    monkeypatch.setattr(prowlarr_service.httpx, 'get', _fake_get)

    settings = SimpleNamespace(host='http://prowlarr', api_key='test-key')
    result = prowlarr_service.search(settings, 'The Gorge 2025 1080p', categories=[2000])

    assert result == []
    assert captured['url'] == 'http://prowlarr/api/v1/search'
    assert captured['params']['query'] == 'The Gorge 2025 1080p'
    assert captured['params']['limit'] == 100
    assert captured['params']['categories'] == [2000]
