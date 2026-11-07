"""タグのジャンル表記を、分類に使うジャンルの候補に正規化する。"""

import re

from volumio_playlist_studio.models import Genre

# 上から順に照合する。「Heavy Metal Rock」をメタル、「Pop Rock」をロックとするため、具体的なものを先に置く
_GENRE_PATTERNS: tuple[tuple[Genre, re.Pattern[str]], ...] = tuple(
    (genre, re.compile(pattern, re.IGNORECASE))
    for genre, pattern in (
        (Genre.METAL, r"metal|メタル"),
        (Genre.HIPHOP, r"hip[\s-]?hop|\brap\b|ラップ|ヒップホップ"),
        (Genre.RNB_SOUL, r"r\s?&\s?b|\brnb\b|soul|funk|gospel|motown|ソウル|ファンク"),
        (
            Genre.ELECTRONIC,
            r"electro|techno|house|trance|\bedm\b|dubstep|drum\s?(and|&|n)\s?bass|synth|テクノ|エレクトロ",
        ),
        (Genre.JAZZ, r"jazz|swing|bossa|fusion|ジャズ|ボサ"),
        (Genre.CLASSICAL, r"classical|baroque|opera|symphon|concerto|クラシック|バロック|オペラ|交響"),
        (Genre.AMBIENT, r"ambient|new\s?age|healing|アンビエント|ヒーリング|ニューエイジ"),
        (Genre.SOUNDTRACK, r"soundtrack|\bost\b|\bscore\b|anime|game|サウンドトラック|サントラ|アニメ|ゲーム"),
        (
            Genre.FOLK_ACOUSTIC,
            r"folk|acoustic|country|bluegrass|singer[\s-]?songwriter|フォーク|アコースティック|カントリー",
        ),
        (Genre.WORLD, r"world|latin|reggae|celtic|african|ワールド|ラテン|レゲエ|民謡|沖縄"),
        (Genre.ROCK, r"rock|punk|grunge|indie|alternative|ロック|パンク|オルタナ"),
        (Genre.POP, r"pop|idol|歌謡|ポップ|アイドル"),
    )
)


def genre_from_tag(tag: str | None) -> Genre | None:
    """タグのジャンル表記を正規化する。

    どの候補にも当てはまらない表記は、推測で「その他」とせず None（不明）を返す。
    """
    if tag is None or not tag.strip():
        return None

    for genre, pattern in _GENRE_PATTERNS:
        if pattern.search(tag):
            return genre

    return None
