"""Jev による、アルバムが子供向けかどうかの判定。

1 アルバムにつき 1 リクエストで、アルバム名・アーティスト名・ジャンルタグ・収録曲名の一覧だけを送る。
曲単位で聞くより、収録曲名の並びから判断できるため精度が見込め、リクエスト数も少なくて済む。
音声ファイル・ファイルパス・音響特徴量は送らない。

結果は確率として保存し、選曲時に `JevSettings.children_threshold` と比べて子供向けかを決める。
閾値を変えても再判定は不要。プレイリストの生成時には Jev を呼ばない。
"""

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

from typesafe_sdk import ContentValue, Noul, SystemOneResponse, TypeSafeClient

from volumio_playlist_studio.classify.jev import JevResponseError
from volumio_playlist_studio.config import JevSettings
from volumio_playlist_studio.models import Track

CHILDREN_QUESTION = "for_children"
# 送る収録曲名とアーティスト名の上限。ボックスセットなどで送信量が増えすぎないようにする
MAX_TRACK_TITLES = 40
MAX_ARTISTS = 10

_STATE_NOTE = (
    "This is one album (or one folder of tracks) from a personal music library, ripped from CDs. "
    "Tags may be missing or partly wrong. The listener wants to keep albums for young children "
    "out of ordinary playlists."
)


@dataclass(frozen=True)
class AlbumJudgment:
    """Jev がアルバムについて判定した結果。

    Attributes:
        children_probability: 子供向けのアルバムである確率（0〜1）。
        model: 応答したモデル名。
        input_tokens: 入力トークン数。
        output_tokens: 出力トークン数。
    """

    children_probability: float
    model: str | None
    input_tokens: int
    output_tokens: int


def build_album_questions() -> dict[str, Noul]:
    """アルバムの質問を作る。質問を変えた場合は JEV_ALBUM_QUESTION_VERSION を更新する。"""
    return {
        CHILDREN_QUESTION: Noul(
            instructions=(
                "This album is mainly intended for young children: for example nursery rhymes, children's songs, "
                "lullabies for babies, songs from TV programs or anime made for preschool or elementary school "
                "children, or educational songs for kids. Anime or game music aimed at teenagers or adults, "
                "and ordinary albums that only contain one or two songs for children, are not"
            ),
        ),
    }


def build_album_state(tracks: list[Track], settings: JevSettings) -> dict[str, ContentValue]:
    """Jev に送る state を作る。ファイルパスなど、判定に不要な情報は含めない。

    Raises:
        ValueError: 曲が 1 つもない場合。
    """
    if not tracks:
        raise ValueError("album has no tracks")

    album = _most_common(t.album for t in tracks)
    year = _most_common(None if t.year is None else str(t.year) for t in tracks)
    info: dict[str, ContentValue] = {"track_count": len(tracks)}
    if album:
        info["title"] = album

    artists = _distinct(t.artist for t in tracks)[:MAX_ARTISTS]
    if artists:
        info["artists"] = artists

    genres = _distinct(t.genre for t in tracks)
    if genres:
        info["genre_tags"] = genres

    if year:
        info["release_year"] = year

    if settings.send_titles:
        titles = _distinct(t.title for t in tracks)[:MAX_TRACK_TITLES]
        if titles:
            info["track_titles"] = titles

    return {"album": info, "note": _STATE_NOTE}


def _distinct(values: Iterable[str | None]) -> list[str]:
    """空の値を除き、最初に現れた順に重複を除く。"""
    return list(dict.fromkeys(_present(values)))


def _most_common(values: Iterable[str | None]) -> str | None:
    counts = Counter(_present(values))
    return counts.most_common(1)[0][0] if counts else None


def _present(values: Iterable[str | None]) -> list[str]:
    return [v for v in values if v is not None and v.strip()]


def parse_album_response(response: SystemOneResponse) -> AlbumJudgment:
    """Jev の応答を判定結果に変換する。

    Raises:
        JevResponseError: 必要な回答がない、または想定外の値が返された場合。
    """
    try:
        probability = float(response.nouls[CHILDREN_QUESTION].noul)
    except (KeyError, ValueError) as exc:
        # 応答の内容はログに残さず、どの回答が不正だったかだけを伝える
        raise JevResponseError(f"missing or invalid answer: {exc.__class__.__name__}") from exc

    if not 0.0 <= probability <= 1.0:
        raise JevResponseError("missing or invalid answer: probability out of range")

    return AlbumJudgment(
        children_probability=probability,
        model=response.model,
        input_tokens=response.usage.input_tokens or 0,
        output_tokens=response.usage.output_tokens or 0,
    )


def judge_album(client: TypeSafeClient, tracks: list[Track], settings: JevSettings) -> AlbumJudgment:
    """アルバムを Jev に送り、子供向けかを判定する。リクエストは 1 回。

    Raises:
        typesafe_sdk.TypeSafeError: 通信・認証・レート制限・応答形式のエラー。
        JevResponseError: 必要な回答がない、または想定外の値が返された場合。
    """
    response = client.system_one(
        state=build_album_state(tracks, settings),
        questions=build_album_questions(),
        model=settings.model,
    )
    return parse_album_response(response)
