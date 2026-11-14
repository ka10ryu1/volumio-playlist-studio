"""アルバム単位のまとまり。子供向けかどうかはアルバム単位で判定し、結果を収録曲すべてに適用する。"""

import json
from collections import defaultdict

from volumio_playlist_studio.models import Track


def album_key(track: Track) -> str:
    """同じアルバムとみなすためのキー。曲のフォルダ（音楽ライブラリのルートからの相対パス）とアルバムタグの組。

    CD を取り込んだライブラリでは、1 枚のアルバムが 1 つのフォルダに入っていることが多い。ただしフォルダだけでは、
    ライブラリの直下などに複数のアルバムの曲が混在する場合を区別できないため、アルバムタグと組み合わせる。
    DB にだけ保存し、Jev には送らない（ファイルパスを含むため）。
    """
    folder = track.rel_path.rpartition("/")[0]
    return json.dumps([folder, track.album or ""], ensure_ascii=False)


def group_albums(tracks: list[Track]) -> dict[str, list[Track]]:
    """曲をアルバムごとにまとめる。アルバム内の曲はトラック番号順（番号がなければ後ろ）に並べる。"""
    albums: dict[str, list[Track]] = defaultdict(list)
    for track in tracks:
        albums[album_key(track)].append(track)

    return {
        key: sorted(members, key=lambda t: (t.track_number is None, t.track_number or 0, t.rel_path))
        for key, members in sorted(albums.items())
    }
