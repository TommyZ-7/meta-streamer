# 利用案内 (meta-streamer / TopazChat互換)

## できること
- `ezStreamer` / OBS からRTMPで配信し、VRChat内の動画プレイヤーで低遅延再生する。
- 中継のみ (無変換)。録画はしない。

## 配信方法
1. 配信キー (推測不能な文字列、例: `9f3a-...` のUUID) を決める。`test-123`のような例示キーは使わない。
2. OBS/ezStreamer: サーバ `rtmp://live.meta-note-ex.com/live`、キーに上記を指定。
3. エンコード目安: 映像 H.264 ≤2000kbps、音声 AAC ≤320kbps、GOP 2秒、Bフレーム 0 (x264 `zerolatency` / NVENC Low Latency推奨)。

## 視聴方法
- PC: `rtspt://live.meta-note-ex.com/live/{キー}` (AVPro Low Latency ON)
- Quest: `rtsp://live.meta-note-ex.com/live/{キー}`
- VRChat設定で `Allow Untrusted URLs` ONが必要な場合がある。

## 重要: キーの性質 (必読)
- **視聴者にキーを渡す = 配信権限も渡す。** キーを知る者は誰でも同じキーで配信できる。
- **同じキーへの後からの配信が先行配信を上書きする。** 乗っ取り・事故時はキーを変えて再配信する。
- キー漏洩時・荒らし被害時は新しいキーに変える。旧キーの「予約」はできない。
- 合計ビットレートが上限 (既定2500kbps) を約15秒超え続けるとサーバが配信を切断する。設定を見直すこと。

## 通信量
- 目安: 視聴者1人あたり約0.9GB/h (2Mbps時)。日別実績は公開ページ参照。
- ベストエフォート提供: 単一サーバ運用のため、混雑・障害時は途切れることがある。重要配信の唯一の手段にしないこと。

## 問い合わせ・通報
- 利用規約・通報窓口は `terms.md` 参照。
