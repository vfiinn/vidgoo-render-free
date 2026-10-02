"""Child-process fixture: real gallery-dl downloader, mocked post extraction only."""
import re
import sys
from pathlib import Path
from unittest.mock import patch

import gallery_dl
from gallery_dl.extractor.common import Extractor, Message

import bot


if __name__ == "__main__":
    destination, local_url = sys.argv[1:]
    post_url = "https://www.instagram.com/p/fixture/"

    class FixtureExtractor(Extractor):
        category = "instagram"
        subcategory = "post"
        request_interval = 0

        def items(self):
            yield Message.Directory, None, {}
            # The file filter must not download this movie or count it against the photo range.
            yield Message.Url, local_url + "/movie.mp4", {"num": 0, "extension": "mp4"}
            for i in range(1, 23):
                yield Message.Url, local_url + "/photo.png", {"num": i, "extension": "png"}

    with patch.object(sys, "argv", ["gallery-dl", *bot.build_gallery_command(post_url, Path(destination))[3:]]), patch.object(
        gallery_dl.extractor, "find", side_effect=lambda *args, **kwargs: FixtureExtractor(re.match(r".*", post_url))
    ):
        raise SystemExit(gallery_dl.main())
