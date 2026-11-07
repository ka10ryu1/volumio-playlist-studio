"""プレイリスト作成画面の「Volumio に登録」部分。

実機と通信するクライアントは未実装のため、現在はモッククライアントで照合と登録の流れを確認する。
"""

import streamlit as st

from volumio_playlist_studio.gui.context import PageContext
from volumio_playlist_studio.gui.widgets import track_label
from volumio_playlist_studio.models import Track
from volumio_playlist_studio.storage.database import transaction, utc_now
from volumio_playlist_studio.storage.playlists import (
    RegistrationRecord,
    SavedPlaylist,
    list_playlists,
    list_registrations,
    save_registration,
)
from volumio_playlist_studio.storage.tracks import get_track, list_tracks
from volumio_playlist_studio.volumio import (
    MatchStatus,
    MockVolumioClient,
    PlaylistExistsError,
    VolumioError,
    collect_track_uris,
    match_tracks_to_uris,
    register_playlist,
)

_MOCK_KEY = "volumio_mock_client"
# モックが模擬する Volumio 上の保存場所。実機の URI は実機のライブラリを参照して確認する
_MOCK_STORAGE_PREFIX = "music-library/INTERNAL"
_STATUS_LABELS = {
    MatchStatus.MATCHED: "対応あり",
    MatchStatus.UNMATCHED: "対応なし",
    MatchStatus.AMBIGUOUS: "候補が複数",
}


def _mock_client(library_root: str, tracks: list[Track]) -> MockVolumioClient:
    """セッション中に使い続けるモッククライアント。登録したプレイリストをセッションの間保持する。"""
    cached: tuple[str, MockVolumioClient] | None = st.session_state.get(_MOCK_KEY)
    if cached is not None and cached[0] == library_root:
        return cached[1]

    client = MockVolumioClient.from_track_uris([f"{_MOCK_STORAGE_PREFIX}/{t.rel_path}" for t in tracks])
    st.session_state[_MOCK_KEY] = (library_root, client)
    return client


def render(ctx: PageContext, saved_playlist_id: int | None) -> None:
    """Volumio への登録欄を表示する。"""
    playlists = list_playlists(ctx.conn)
    if not playlists:
        return

    with st.expander("Volumio に登録", expanded=saved_playlist_id is not None):
        st.info(
            "Volumio 実機との通信は未実装です。現在はモッククライアント（Volumio のライブラリを模擬）で、"
            "URI の照合から登録までの流れを確認できます。実機の Volumio は変更されません。"
        )
        ids = [p.id for p in playlists]
        default = ids.index(saved_playlist_id) if saved_playlist_id in ids else 0
        playlist = st.selectbox("登録するプレイリスト", playlists, index=default, format_func=lambda p: p.name)
        if playlist is not None:
            _preview_and_register(ctx, playlist)


def _preview_and_register(ctx: PageContext, playlist: SavedPlaylist) -> None:
    tracks = [t for t in (get_track(ctx.conn, e.track_id) for e in playlist.entries) if t is not None]
    client = _mock_client(ctx.library_root or "", list_tracks(ctx.conn, ctx.library_root or ""))
    try:
        uris = collect_track_uris(client)
    except VolumioError:
        st.error("Volumio のライブラリを取得できませんでした。")
        return

    matches = match_tracks_to_uris(tracks, uris)

    st.markdown("**登録前のプレビュー**")
    st.dataframe(
        [
            {"No.": i + 1, "曲": track_label(t), "照合": _STATUS_LABELS[m.status], "Volumio の URI": m.uri or ""}
            for i, (t, m) in enumerate(zip(tracks, matches, strict=True))
        ],
        hide_index=True,
        width="stretch",
    )
    unmatched = sum(1 for m in matches if m.status != MatchStatus.MATCHED)
    if unmatched:
        st.warning(f"{unmatched} 曲は Volumio 上の URI と一意に対応しないため、登録されません。")

    name = st.text_input("Volumio のプレイリスト名", value=playlist.name, key=f"volumio-name-{playlist.id}")
    if name.strip() in client.list_playlists():
        st.warning("同じ名前のプレイリストが既にあります。既存のプレイリストは変更しないため、別の名前にしてください。")

    confirmed = st.checkbox("この内容で新しいプレイリストを作成します", key=f"volumio-confirm-{playlist.id}")
    if st.button("Volumio に登録", disabled=not confirmed, key=f"volumio-register-{playlist.id}"):
        _register(ctx, client, playlist, name, [(t.id, m.uri) for t, m in zip(tracks, matches, strict=True)])

    history = list_registrations(ctx.conn, playlist.id)
    if history:
        st.markdown("**登録履歴**")
        for record in history:
            added = sum(1 for i in record.items if i.status == "added")
            st.caption(
                f"{record.started_at}　{record.volumio_playlist_name}　{record.status}　"
                f"{added}/{len(record.items)} 曲（{record.client_kind}）"
            )


def _register(
    ctx: PageContext,
    client: MockVolumioClient,
    playlist: SavedPlaylist,
    name: str,
    entries: list[tuple[int, str | None]],
) -> None:
    started_at = utc_now()
    try:
        outcome = register_playlist(client, name, entries)
    except PlaylistExistsError:
        st.error("同じ名前のプレイリストが既にあるため、登録しませんでした。")
        return
    except ValueError:
        st.error("プレイリスト名が空か、登録できる曲がありません。")
        return
    except VolumioError as exc:
        st.error(f"Volumio との通信に失敗しました: {exc}")
        return

    with transaction(ctx.conn):
        save_registration(
            ctx.conn,
            RegistrationRecord(
                playlist.id, name.strip(), client.kind, outcome.status, outcome.error, started_at, outcome.items
            ),
        )

    added = sum(1 for i in outcome.items if i.status == "added")
    if outcome.status == "success":
        st.success(f"{added} 曲を登録しました。")
    else:
        st.error(f"登録が完了しませんでした（{added}/{len(outcome.items)} 曲を登録）。{outcome.error or ''}")
        st.dataframe(
            [{"No.": i.position + 1, "状態": i.status, "内容": i.error or ""} for i in outcome.items],
            hide_index=True,
            width="stretch",
        )
