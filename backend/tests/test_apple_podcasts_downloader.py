import tempfile
import unittest
from pathlib import Path
import sys
from unittest.mock import MagicMock, Mock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.downloaders.apple_podcasts_downloader import ApplePodcastsDownloader


class TestApplePodcastsDownloader(unittest.TestCase):
    URL = "https://podcasts.apple.com/cn/podcast/example/id1498541229?i=1000773193034"

    def test_download_uses_episode_metadata_and_audio_url(self):
        lookup_response = Mock()
        lookup_response.raise_for_status.return_value = None
        lookup_response.json.return_value = {
            "results": [{
                "trackId": 1000773193034,
                "trackName": "Episode title",
                "trackTimeMillis": 123000,
                "artworkUrl600": "https://example.com/cover.jpg",
                "episodeUrl": "https://example.com/audio.mp3",
            }]
        }
        audio_response = MagicMock()
        audio_response.__enter__.return_value = audio_response
        audio_response.__exit__.return_value = False
        audio_response.raise_for_status.return_value = None
        audio_response.iter_content.return_value = [b"audio"]

        with tempfile.TemporaryDirectory() as output_dir, patch(
            "app.downloaders.apple_podcasts_downloader.requests.get",
            side_effect=[lookup_response, audio_response],
        ) as get:
            result = ApplePodcastsDownloader().download(self.URL, output_dir=output_dir)

            self.assertEqual(result.video_id, "1000773193034")
            self.assertEqual(result.title, "Episode title")
            self.assertEqual(result.duration, 123)
            self.assertEqual(result.platform, "apple_podcasts")
            self.assertEqual(Path(result.file_path).read_bytes(), b"audio")
            self.assertEqual(get.call_args_list[0].kwargs["params"]["country"], "cn")

    def test_rejects_show_url_without_episode_id(self):
        with self.assertRaisesRegex(ValueError, "单集"):
            ApplePodcastsDownloader().download(
                "https://podcasts.apple.com/cn/podcast/example/id1498541229",
                skip_download=True,
            )
