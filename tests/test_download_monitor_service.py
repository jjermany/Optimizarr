from types import SimpleNamespace

from app.models.library import DownloadQualityProfileEnum
from app.services.download_monitor_service import _select_best_release



def _profile(quality: DownloadQualityProfileEnum):
    return SimpleNamespace(target_resolution=1080, download_quality_profile=quality.value)



def test_select_best_release_web_dl_rejects_webrip_only_titles():
    releases = [
        {
            'title': 'Movie.2024.1080p.WEBRip.x265-GROUP',
            'seeders': 999,
            'size': 1000,
            'protocol': 'torrent',
        }
    ]

    selected = _select_best_release(releases, _profile(DownloadQualityProfileEnum.web_dl), qbt_enabled=True, sab_enabled=True)

    assert selected is None



def test_select_best_release_mixed_tags_resolve_deterministically_for_web_dl():
    releases = [
        {
            'title': 'Movie.2024.1080p.WEBRip.WEB-DL.x265-GROUP',
            'seeders': 10,
            'size': 1500,
            'protocol': 'torrent',
        },
        {
            'title': 'Movie.2024.1080p.WEBRip.x265-OTHER',
            'seeders': 500,
            'size': 1200,
            'protocol': 'torrent',
        },
    ]

    selected = _select_best_release(releases, _profile(DownloadQualityProfileEnum.web_dl), qbt_enabled=True, sab_enabled=True)

    assert selected is not None
    assert selected['title'] == 'Movie.2024.1080p.WEBRip.WEB-DL.x265-GROUP'



def test_select_best_release_any_allows_all_quality_classes():
    releases = [
        {
            'title': 'Movie.2024.1080p.WEBRip.x265-HIGHSEED',
            'seeders': 500,
            'size': 1400,
            'protocol': 'torrent',
        },
        {
            'title': 'Movie.2024.1080p.BluRay.x265-LOWSEED',
            'seeders': 50,
            'size': 1100,
            'protocol': 'torrent',
        },
    ]

    selected = _select_best_release(releases, _profile(DownloadQualityProfileEnum.any), qbt_enabled=True, sab_enabled=True)

    assert selected is not None
    assert selected['title'] == 'Movie.2024.1080p.WEBRip.x265-HIGHSEED'
