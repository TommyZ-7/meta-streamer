# 運用runbook (VM上 `/opt/meta-streamer` で実行)

## 死活確認 (日常)
```bash
docker compose ps                      # mediamtx/watchdog/traffic が up/healthy か
docker inspect -f '{{.State.Health.Status}}' meta-watchdog   # healthy 期待
docker compose logs --tail=50 watchdog | grep -E 'over limit|KICK|fatal' || true
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1/traffic.json  # 200 期待
crontab -l | grep -E 'traffic.py|watchdog-health'  # cron 3行あること
```

## 荒らし配信の停止 (手動kick)
```bash
# 1. 配信中パスとpublisherの接続IDを確認
scripts/mtx-api.sh GET '/v3/paths/list?itemsPerPage=1000'
# 対象パスの source.id / source.type を控える (例: rtmpConn / <uuid>)
# 2. kick (source.type に対応するendpointへ)
scripts/mtx-api.sh POST /v3/rtmp/conns/kick/<id>   # RTMP配信者
scripts/mtx-api.sh POST /v3/rtsp/sessions/kick/<id> # RTSP配信者
# 3. 被害者にキー変更を案内 (旧キーの予約機能はない)
```
注意: kickは一時的。再発時はIPブロックへ。

## IPブロック (再発する荒らし)
1. `docker compose logs mediamtx` 等から送信元IPを特定。
2. OCIコンソール: VCN Security ListのIngressから該当IPを除外 (deny規則または該当許可の見直し)。
3. 緊急時はVM側 `sudo iptables -I INPUT -s <IP> -j DROP` (再起動で消える一時的措置。恒久はVCN側)。

## 再起動・復旧
```bash
docker compose restart watchdog   # watchdog不調時 (通常はhealth.shが自動実施)
docker compose up -d              # 全体再起動 (配信は一瞬切れる)
docker compose pull && docker compose up -d  # 更新時
```
- mediamtx再作成中は全配信が切断される。視聴者は再読込、配信者は再配信で復帰。
- 切り戻し: `/opt/meta-streamer.bak` (更新前に取得) に戻して `up -d`。

## ログ場所
- `docker compose logs -f [mediamtx|watchdog|traffic]`
- VM: `/opt/meta-streamer/traffic-cron.log`, `watchdog-health.log`

## バックアップ
- ステートレス (設定ファイルのみ)。更新前に `/opt/meta-streamer` 全体と `.env` を退避すれば足りる。
- 履歴CSV (`data/traffic-daily.csv`) は消すと公開ページの過去日別が欠ける。退避対象に含める。

## 定期cron (provision.shが登録)
- `*/5` traffic render、`55 23` snapshot、`*/2` watchdog-health。
