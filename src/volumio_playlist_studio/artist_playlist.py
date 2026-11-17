"""アーティストを指定してプレイリストを編成する。

指定したアーティストの曲だけを候補にし、選曲・曲順は「雰囲気と長さで選ぶ」（`playlist.generate_playlist`）と
同じ方法で決める。雰囲気は任意で、「おまかせ」なら雰囲気で絞り込まないため、未解析・未分類の曲も選べる。

アーティスト名はタグの値で比べ、前後の空白と大文字・小文字の違いだけを同一視する。
「A feat. B」のような表記は A とは別のアーティストとして扱う（意図しない曲が混ざらないようにするため）。
"""

from collections import Counter
from dataclasses import dataclass, replace

from volumio_playlist_studio.config import PlaylistDefaults
from volumio_playlist_studio.models import Mood, Track
from volumio_playlist_studio.playlist import (
    REASON_CHILDREN,
    REASON_UNCLASSIFIED,
    REASON_UNSUITABLE,
    Candidate,
    ChildrenMode,
    CreationMethod,
    GenerationResult,
    PlaylistRequest,
    Shortfall,
    generate_playlist,
)


@dataclass(frozen=True)
class ArtistSummary:
    """選曲の候補に含まれるアーティスト。

    Attributes:
        name: 表示名。同じアーティストに複数の表記があれば、最も多い表記（同数なら文字列順で先の表記）。
        track_count: 曲数。
    """

    name: str
    track_count: int


def artist_key(name: str) -> str:
    """同じアーティストとみなすためのキー。"""
    return name.strip().casefold()


def _track_artist_key(track: Track) -> str | None:
    """曲のアーティストのキー。アーティストが不明（タグがない・空白だけ）なら None。"""
    if track.artist is None or not track.artist.strip():
        return None

    return artist_key(track.artist)


def list_artists(candidates: list[Candidate]) -> list[ArtistSummary]:
    """候補に含まれるアーティストを、名前順に返す。アーティストが不明な曲は含めない。"""
    spellings: dict[str, Counter[str]] = {}
    for candidate in candidates:
        key = _track_artist_key(candidate.track)
        if key is not None and candidate.track.artist is not None:
            spellings.setdefault(key, Counter())[candidate.track.artist.strip()] += 1

    summaries = [
        ArtistSummary(
            name=min(counts, key=lambda spelling: (-counts[spelling], spelling)),
            track_count=counts.total(),
        )
        for counts in spellings.values()
    ]
    return sorted(summaries, key=lambda s: (artist_key(s.name), s.name))


@dataclass(frozen=True)
class ArtistRequest(PlaylistRequest):
    """アーティストで選ぶプレイリストの生成条件。

    雰囲気・長さ・詳細設定は「雰囲気と長さで選ぶ」と同じ意味を持つ。雰囲気を「おまかせ」にすると雰囲気で絞り込まない。

    Attributes:
        artists: 対象のアーティスト名（画面で選んだ表示名）。大文字・小文字の違う表記の曲も対象にする。
    """

    mood: Mood = Mood.OMAKASE
    artists: tuple[str, ...] = ()

    def validate(self, defaults: PlaylistDefaults) -> None:
        """条件を検証する。

        Raises:
            ValueError: 条件が不正な場合。
        """
        super().validate(defaults)
        if not self.artists or any(not artist_key(a) for a in self.artists):
            raise ValueError("at least one non-empty artist must be specified")

    def to_dict(self) -> dict[str, object]:
        """保存用の辞書に変換する。"""
        data = super().to_dict()
        data["method"] = CreationMethod.ARTIST.value
        data["artists"] = list(self.artists)
        return data


def generate_artist_playlist(
    candidates: list[Candidate], request: ArtistRequest, defaults: PlaylistDefaults
) -> GenerationResult[ArtistRequest]:
    """指定したアーティストの曲から選び、曲順を決める。

    Args:
        candidates: 選曲の候補（ライブラリ全体）。
        request: 生成条件。
        defaults: 生成の既定値と制約。

    Returns:
        生成結果。曲が足りない場合も、集まった曲と不足の説明を返す。不足の内訳には、
        指定外のアーティストの曲は数えない。

    Raises:
        ValueError: 条件が不正な場合。
    """
    request.validate(defaults)
    keys = {artist_key(a) for a in request.artists}
    matched = [c for c in candidates if _track_artist_key(c.track) in keys]
    # アーティストが 1 組だけなら連続は避けようがないため、連続を避けられない旨の警告を出さない
    base_request = request if len(keys) > 1 else replace(request, avoid_same_artist=False)
    base = generate_playlist(matched, base_request, defaults)
    shortfall = base.shortfall
    if shortfall is not None:
        shortfall = replace(shortfall, suggestions=[*_suggestions(request, shortfall), *shortfall.suggestions])

    return GenerationResult(request=request, tracks=base.tracks, shortfall=shortfall, warnings=base.warnings)


def _suggestions(request: ArtistRequest, shortfall: Shortfall) -> list[str]:
    """アーティストで選ぶ場合に固有の緩和案。共通の緩和案より先に提示する。"""
    suggestions = ["アーティストを追加する"]
    if request.mood != Mood.OMAKASE and (
        shortfall.excluded.get(REASON_UNSUITABLE) or shortfall.excluded.get(REASON_UNCLASSIFIED)
    ):
        suggestions.append("雰囲気を「おまかせ」にする（雰囲気で絞り込まない）")

    if request.children_mode == ChildrenMode.EXCLUDE and shortfall.excluded.get(REASON_CHILDREN):
        suggestions.append("子供向けの曲を「含める」にする")

    return suggestions
