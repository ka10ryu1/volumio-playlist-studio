"""プレイリスト作成画面の「Volumio に登録」部分。

照合先は次の 2 つから選ぶ。
- Volumio 実機: REST API で取得した曲の一覧と照合してプレビューを表示し、確認のうえ WebSocket API で登録する。
  登録は通信に時間がかかるため、バックグラウンド処理で行う
- モック: Volumio のライブラリを模擬し、照合から登録までの流れを確認する。実機は変更しない
"""

import streamlit as st

from volumio_playlist_studio.gui.context import PageContext, running_job, start_job
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
from volumio_playlist_studio.storage.volumio_library import last_collected_at, load_uris
from volumio_playlist_studio.volumio import (
    MatchStatus,
    MockVolumioClient,
    PlaylistExistsError,
    UriMatch,
    VolumioError,
    collect_track_uris,
    match_tracks_to_uris,
    register_playlist,
)
from volumio_playlist_studio.workflows import VolumioRegistrationRequest, run_volumio_register, run_volumio_sync

_MOCK_KEY = "volumio_mock_client"
_SOURCE_REAL = "Volumio 実機"
_SOURCE_MOCK = "モック（模擬）"
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
        ids = [p.id for p in playlists]
        default = ids.index(saved_playlist_id) if saved_playlist_id in ids else 0
        playlist = st.selectbox("登録するプレイリスト", playlists, index=default, format_func=lambda p: p.name)
        if playlist is None:
            return

        host = ctx.volumio_host
        sources = [_SOURCE_REAL, _SOURCE_MOCK] if host else [_SOURCE_MOCK]
        source = st.radio("照合先", sources, horizontal=True, key="volumio-source")
        if host is None:
            st.caption("Volumio の接続先を設定画面で設定すると、Volumio 実機のライブラリと照合できます。")

        tracks = [t for t in (get_track(ctx.conn, e.track_id) for e in playlist.entries) if t is not None]
        if source == _SOURCE_REAL and host is not None:
            _real_preview_and_register(ctx, host, playlist, tracks)
        else:
            _mock_preview_and_register(ctx, playlist, tracks)

        _history(ctx, playlist)


def _real_preview_and_register(ctx: PageContext, host: str, playlist: SavedPlaylist, tracks: list[Track]) -> None:
    """Volumio 実機から取得した曲の一覧と照合してプレビューを表示し、確認のうえ実機に登録する。"""
    st.info(
        "曲の一覧の取得と照合では、Volumio は変更しません。「Volumio に登録」を押したときだけ、"
        "Volumio 実機に新しいプレイリストを作成します（既存のプレイリストは変更しません）。"
    )
    collected_at = last_collected_at(ctx.conn, host)
    st.caption(f"接続先: {host}　曲の一覧の取得: {collected_at or '未取得'}")
    if st.button("Volumio から曲の一覧を取得", disabled=running_job() is not None):
        start_job(
            "Volumio の曲の一覧の取得",
            ctx.config.database_path,
            lambda conn, control: run_volumio_sync(conn, host, control),
        )
        st.rerun()

    uris = load_uris(ctx.conn, host)
    if not uris:
        st.caption("まだ曲の一覧を取得していません。")
        return

    matches = match_tracks_to_uris(tracks, uris)
    _preview(tracks, matches)
    st.caption("Volumio のライブラリを変更した後は、曲の一覧を取得し直してから登録してください。")
    job_running = running_job() is not None
    registrable = any(m.status == MatchStatus.MATCHED for m in matches)
    name = _registration_form(playlist, "real", disabled=job_running or not registrable)
    if job_running:
        st.caption("実行中の処理が終わると登録できます。")

    if name is None:
        return

    request = VolumioRegistrationRequest(
        host, playlist.id, name, [(t.id, m.uri) for t, m in zip(tracks, matches, strict=True)]
    )
    start_job(
        "Volumio へのプレイリストの登録",
        ctx.config.database_path,
        lambda conn, control: run_volumio_register(conn, request, control),
    )
    st.rerun()


def _registration_form(
    playlist: SavedPlaylist, source_key: str, *, existing: list[str] | None = None, disabled: bool = False
) -> str | None:
    """プレイリスト名の入力・確認のチェック・登録ボタンを表示し、ボタンが押されたら入力した名前を返す。

    照合先を切り替えたときに確認のチェックを引き継がないよう、照合先ごとに別のキーを使う。

    Args:
        playlist: 登録するプレイリスト。
        source_key: 照合先を区別するキー。
        existing: 既存のプレイリスト名。分かる場合だけ渡し、同じ名前なら警告する（登録時にも確認する）。
        disabled: 登録ボタンを無効にするかどうか。
    """
    name = st.text_input(
        "Volumio のプレイリスト名", value=playlist.name, key=f"volumio-name-{source_key}-{playlist.id}"
    )
    if existing is not None and name.strip() in existing:
        st.warning("同じ名前のプレイリストが既にあります。既存のプレイリストは変更しないため、別の名前にしてください。")

    confirmed = st.checkbox(
        "この内容で新しいプレイリストを作成します", key=f"volumio-confirm-{source_key}-{playlist.id}"
    )
    if st.button(
        "Volumio に登録", disabled=disabled or not confirmed, key=f"volumio-register-{source_key}-{playlist.id}"
    ):
        return name

    return None


def _mock_preview_and_register(ctx: PageContext, playlist: SavedPlaylist, tracks: list[Track]) -> None:
    st.info(
        "モッククライアント（Volumio のライブラリを模擬）で、照合から登録までの流れを確認します。実機は変更しません。"
    )
    client = _mock_client(ctx.library_root or "", list_tracks(ctx.conn, ctx.library_root or ""))
    try:
        uris = collect_track_uris(client)
    except VolumioError:
        st.error("Volumio のライブラリを取得できませんでした。")
        return

    matches = match_tracks_to_uris(tracks, uris)
    _preview(tracks, matches)

    name = _registration_form(playlist, "mock", existing=client.list_playlists())
    if name is not None:
        _register(ctx, client, playlist, name, [(t.id, m.uri) for t, m in zip(tracks, matches, strict=True)])


def _preview(tracks: list[Track], matches: list[UriMatch]) -> None:
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


def _history(ctx: PageContext, playlist: SavedPlaylist) -> None:
    history = list_registrations(ctx.conn, playlist.id)
    if not history:
        return

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
        st.error("プレイリスト名が空か使えない文字（/ や \\ など）を含むか、登録できる曲がありません。")
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
