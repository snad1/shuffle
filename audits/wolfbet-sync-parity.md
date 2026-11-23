# WolfBet sync parity: Shuffle

What this repo does compared to WolfBet CLI 2.16.0 to 2.37.2 and WolfBet TG v1.21.0, as of Shuffle CLI 1.13.0 and TG 1.10.1 (2026-11-23).

- **Ported**: same behaviour as WolfBet.
- **Adapted**: same intent, changed to fit this casino's API (the note says how).
- **N/A**: the casino API cannot support it; the note gives the API reason.

Paths are relative to this repo. Run the tests with `APP_ENV=testing .venv-test/bin/python -m pytest shuffle/tests -q` from the workspace root.

| WolfBet | Change | Status | Where | Test | Note |
|---|---|---|---|---|---|
| 2.16.0 | recurring restart, streak delays, `streakbet_loss`, `basebet_pct`, milestones | Ported | `shuffle.py:1946`, `shuffle.py:1898`, `shuffle.py:1919`, `tg/engine.py:287` | `tests/test_wolfbet_sync_e2.py::test_streak_reset_restarts_without_recurring` | TG already had it |
| 2.16.0 | 429 Retry-After backoff | Ported | `shuffle.py:1808` | `tests/test_wolfbet_sync_e0.py::test_cli_recovers_from_429_and_timeout` |  |
| 2.17.0 | API last / avg / peak / p50, on-target %, cycle, efficiency | Ported | `shuffle.py:1244`, `shuffle.py:1258`, `shuffle.py:1305` | `tests/test_wolfbet_sync_e5.py::test_record_api_ms_counters_and_on_target` | no rate-limit row: N/A, the API returns no quota headers |
| 2.18.0 | wizard Step 5b + preset keys | Ported | `shuffle.py:3971` | `tests/test_wolfbet_sync_e3.py::test_preset_round_trip` |  |
| 2.19.0 | `T` tweak hotkey, `_apply_mutation` | Ported | `shuffle.py:1495`, `shuffle.py:3159`, `tg/handlers.py:1269` | `tests/test_wolfbet_sync_e5.py::test_show_tweak_applies_and_restores_pause` | no UUID clear: no server auto-bet; TG already had `/tweak` |
| 2.20.0 | exit summary, CSV / JSON export, `E` hotkey | Ported | `shuffle.py:4203`, `shuffle.py:4278`, `shuffle.py:3132` | `tests/test_wolfbet_sync_e6.py::test_export_csv_shape_and_mode` |  |
| 2.21.0 | backtest / replay | Adapted | `shuffle.py:4305`, `shuffle.py:4391`, `shuffle.py:4326` | `tests/test_wolfbet_sync_e6.py::test_backtest_is_deterministic_by_seed` | service house edge and payout |
| 2.22.0 | `core/games.py` WolfBet game registry | N/A | - | - | WolfBet API game list; each service keeps its own game builders |
| 2.22.1 | `_check_risky_config` | Ported | `shuffle.py:1367` | `tests/test_wolfbet_sync_e5.py::test_risky_warns_on_cleartext_proxy_only` |  |
| 2.23.0 | `--analytics` | Ported | `shuffle.py:4751`, `tg/handlers.py:1812` | `tests/test_portability_v1.py::test_cmd_analytics_exists` | TG already had `/analytics` |
| 2.24.0 | `--pipeline N` | N/A | - | - | single-request bet API; concurrent bets break balance tracking |
| 2.25.0 | max-nonce recovery | N/A | - | - | WolfBet seed / nonce API; the service has no max-nonce error |
| 2.25.0 | `--config-show`, `--config-set`, `--version` | Ported | `shuffle.py:5701`, `shuffle.py:5875`, `shuffle.py:6224` | `tests/test_wolfbet_sync_e10.py::test_config_set_scope` |  |
| 2.25.0 | monitor ROI / p99 | Ported | `shuffle.py:1251`, `shuffle.py:5319` | `tests/test_wolfbet_sync_e4.py::test_monitor_header_run_time_only_when_present` |  |
| 2.25.1 - 2.25.2 | ctl subcommands, `--autostash` | Ported | `shufflectl:1079`, `shufflectl:782` | `tests/test_wolfbet_sync_e6.py::test_ctl_syntax` |  |
| 2.25.3 | `recurring_delay` | Ported | `shuffle.py:381` | `tests/test_wolfbet_sync_e2.py::test_recurring_keeps_recurring_delay` | TG already had it (no cap) |
| 2.26.0 | proxy, `purge_days` + `--cleanup` | Ported | `shuffle.py:23`, `shuffle.py:5946`, `shuffle.py:5973` | `tests/test_wolfbet_sync_e5.py::test_purge_days_mutation_clamps_and_off` |  |
| 2.26.1 | `--benchmark`, `--diagnose` | Adapted | `shuffle.py:6032`, `shuffle.py:6105` | `tests/test_portability_v1.py::test_cmd_diagnose_exists` | probe = the service's read-only endpoint |
| 2.27.0 | async bet writer + PRAGMAs | Ported | `shuffle.py:2485`, `shuffle.py:2563` | `tests/test_wolfbet_sync_e5.py::test_writer_batches_and_drains` | PRAGMAs were already there; TG already batched |
| 2.28.0 | helpers moved to `core/` | Adapted | `shuffle.py:15` | `tests/test_portability_v1.py::test_tg_analytics_supports_date_window` | pure helpers live in casino-shared (`shared.core.*`) |
| 2.28.1 | monitor footer hints, systemd-aware stop | Ported | `shuffle.py:4990`, `shuffle.py:5001` | `tests/test_wolfbet_sync_e5.py::test_is_systemd_managed` |  |
| 2.28.2 | config persistence gap | Ported | `shuffle.py:475` | `tests/test_wolfbet_sync_e10.py::test_run_profit_sums_sessions_and_bumps_persist` |  |
| 2.28.3 | SIGHUP live reload | Ported | `shuffle.py:555`, `shuffle.py:614` | `tests/test_wolfbet_sync_e10.py::test_live_reload_increment_off_ends_run` |  |
| 2.28.4 | uptime freezes in recurring pause | Ported | `shuffle.py:1167`, `tg/engine.py:316` | `tests/test_wolfbet_sync_e3.py::test_pause_freezes_uptime_and_reports_remaining` | TG already had it |
| 2.29.0 | profit increment restarts the auto cycle | N/A | - | - | no server auto-bet cycle; the bump already applies to the next bet |
| 2.30.0 | CFG row | Ported | `shuffle.py:2775` | `tests/test_wolfbet_sync_e10.py::test_progress_row_uses_run_total` | pre-existing |
| 2.30.1 | persist `next_profit_milestone`, reset per cycle | Ported | `shuffle.py:459`, `tg/engine.py:386` | `tests/test_wolfbet_sync_e10.py::test_cycle_keeps_run_total_and_milestone` | the CFG `Ms` segment now reads the persisted milestone |
| 2.30.2 | ctl update self-heal | Ported | `shufflectl:1079` | `tests/test_wolfbet_sync_e7.py::test_update_autostashes_reinstalls_and_restarts_what_ran` |  |
| 2.30.3 - 2.30.5 | metric correctness, windowed cycle / efficiency | Ported | `shuffle.py:1272`, `shuffle.py:1305` | `tests/test_wolfbet_sync_e5.py::test_metric_helpers_empty_are_zero` |  |
| wolfctl 3.7 - 3.8 | `doctor [--fix]`, `Nice=-5` | Ported | `shufflectl:1131`, `install.sh:80` | `tests/test_wolfbet_sync_e7.py::test_doctor_warns_on_a_disabled_unit_and_loose_config` |  |
| 2.30.6 | `recurring_delay` cap 86400 + visible clamps | Ported | `shuffle.py:16`, `tg/handlers.py:40` | `tests/test_wolfbet_sync_e2.py::test_recurring_delay_capped_at_stop` | CLI and TG |
| 2.30.7 | `Ms` per-window progress | Ported | `shuffle.py:2792` | `tests/test_wolfbet_sync_e3.py::test_cfg_row_ms_window_rec_and_sr` |  |
| 2.30.8 | `bet_value` / 422 | N/A | - | - | WolfBet bet_value validation; the service has no such field |
| 2.30.9 | ctl `reset` / `archives`, `--stats --since / --until` | Ported | `shufflectl:1132`, `shufflectl:1134`, `shuffle.py:6253` | `tests/test_wolfbet_sync_e7.py::test_reset_archives_and_never_deletes` |  |
| security 2026-11-15 | default web credentials refused, per-user web authz, WS `_connect_ro`, owner gate, `nan` / `inf` limits, pinned `ctl update`, admin secret rendering, API keys out of logs | Adapted | `web/config.py:12`, `web/bot_db.py:9`, `shuffle.py:463` | `tests/test_security_hotfix.py::test_other_user_is_forbidden` | same intent, service code paths |
| security 2026-11-15 | service-specific guards | Ported | `shuffle.py:1700` | `tests/test_security_hotfix.py::test_query_retries_but_mutation_does_not` | no retry on GraphQL mutations |
| 2.31.0 | date-range analytics | Ported | `shuffle.py:4466` | `tests/test_wolfbet_sync_e3.py::test_cfg_row_ms_window_rec_and_sr` | TG already had it |
| 2.32.0 | streak reset | Ported | `shuffle.py:17`, `tg/config.py:47` | `tests/test_wolfbet_sync_e2.py::test_streak_reset_fires_on_breaking_win_at_profit` | CLI and TG |
| 2.32.1 | insufficient balance is terminal on auto | Adapted | `shuffle.py:1104`, `tg/engine.py:371` | `tests/test_wolfbet_sync_e0.py::test_cli_insufficient_balance_stops` | terminal stop + chain-depth warning; mid-chain auto guard N/A (no server auto-bet) |
| 2.33.0 | time-of-day window fix | Ported | `shuffle.py:20` | `tests/test_wolfbet_sync_e4.py::test_stats_time_of_day_window_excludes_earlier_rows` | CLI and TG |
| 2.33.1 | wins / losses per session | Ported | `shuffle.py:322` | `tests/test_wolfbet_sync_e4.py::test_stats_wins_losses_per_session` |  |
| 2.34.0 | durations, time span, run time | Ported | `shuffle.py:20` | `tests/test_wolfbet_sync_e4.py::test_stats_time_span_vs_bet_time_with_gap` |  |
| 2.35.0 | parallel lanes: `--lane K`, laneK paths, `[LK]` prefix | Ported | `shuffle.py:1205` | `tests/test_wolfbet_sync_e5_lanes.py::test_apply_lane_switches_paths_and_log_prefix` |  |
| 2.35.0 | `sessions.lane` column | Ported | `shuffle.py:366` | `tests/test_wolfbet_sync_e4.py::test_init_db_adds_lane_to_old_db_idempotent` | schema lives in casino-shared `SESSIONS_SCHEMA` |
| 2.35.0 | split budget, `Lane budget exceeded` loss stop | Ported | `shuffle.py:1226`, `shuffle.py:26` | `tests/test_wolfbet_sync_e5_lanes.py::test_init_lane_budget_per_mode` |  |
| 2.35.0 | per-lane overrides, `config-set lanes=N` / `laneK.key` | Ported | `shuffle.py:5813` | `tests/test_wolfbet_sync_e5_lanes.py::test_load_config_applies_lane_overrides` |  |
| 2.35.0 | lane start stagger | Ported | `shuffle.py:6386` | `tests/test_wolfbet_sync_e5_lanes.py::test_start_stagger_and_share` |  |
| 2.35.0 | soft pacing from `x-ratelimit-*` headers | N/A | - | - | the service returns no quota headers; each lane keeps the 429 backoff |
| 2.35.0 | `/auto/start` cross-lane lock | N/A | - | - | no server auto-bet |
| 2.35.0 | `pipeline_depth` forbidden key | N/A | - | - | no pipeline |
| 2.35.0 | `--benchmark --lanes M` | Adapted | `shuffle.py:6047` | `tests/test_wolfbet_sync_e5_lanes.py::test_benchmark_lanes_table_and_recommendation` | read-only probe at 1..M concurrent clients |
| 2.35.0 | `--stats` / `--analytics --lane K`, analytics By lane | Ported | `shuffle.py:4676` | `tests/test_wolfbet_sync_e4.py::test_stats_lane_filter_and_tag` |  |
| wolfctl 3.10 | ctl `lanes [N]`, all-lane start / stop / restart / update / reset | Ported | `shufflectl:1113` | `tests/test_wolfbet_sync_e7.py::test_lanes_n_writes_config_and_enables_lane_units` |  |
| 2.35.0 | service-specific lanes | N/A | - | - | nothing account-wide to share |
| 2.36.0 | `lane_split` none / budget / bet / both | Ported | `shuffle.py:502`, `shuffle.py:28` | `tests/test_wolfbet_sync_e5_lanes.py::test_split_default_none_uses_full_bet` | a divided bet below the service minimum stays full |
| wolfctl 3.11 | ctl `lanes N --split MODE` | Ported | `shufflectl:196` | `tests/test_wolfbet_sync_e5_lanes.py::test_split_default_none_uses_full_bet` |  |
| 2.37.0 | `profit_increment_scope` / `piscope` session / run, run file, DB sum, resume bumps | Ported | `shuffle.py:2260`, `shuffle.py:2333`, `shuffle.py:96` | `tests/test_wolfbet_sync_e10.py::test_stale_run_file_after_db_reset_starts_new_run` | CLI; TG adapted by request |
| 2.37.0 | status `Run Profit`, monitor `RunMs` | Ported | `shuffle.py:4955`, `shuffle.py:2792` | `tests/test_wolfbet_sync_e10.py::test_progress_row_uses_run_total` |  |
| 2.37.0 | `--run-reset`, ctl `run-reset`, `reset` clears run files | Ported | `shuffle.py:2418`, `shufflectl:1133`, `tg/handlers.py:2429` | `tests/test_wolfbet_sync_e10.py::test_run_reset_removes_files` |  |
| 2.37.0 | auto-UUID restart on bump | N/A | - | - | no server auto-bet; the bumped base applies to the next bet |
| 2.37.1 | UUID swap waits for non-negative profit and streak | N/A | - | `tests/test_wolfbet_sync_e8_regression.py::test_cli_bump_on_a_loss_keeps_betting_martingale` | no server auto-bet, UUID or mid-chain guard; proven by the regression test instead |
| 2.37.1 | `Stop reason` row + `DAEMON EXIT ... reason=` | Ported | `shuffle.py:4224`, `shuffle.py:6468` | `tests/test_wolfbet_sync_e6.py::test_summary_shows_the_stop_reason` |  |
| 2.37.1 | `install.sh` enables the CLI unit (boot only) | Ported | `install.sh:190` | `tests/test_wolfbet_sync_e7.py::test_lanes_n_writes_config_and_enables_lane_units` | lane units too; `doctor` flags a disabled unit |
| 2.37.2 | bumps belong to the base they were earned on | Ported | `shuffle.py:2403`, `shuffle.py:520` | `tests/test_wolfbet_sync_e10.py::test_live_base_change_zeroes_bumps_and_reanchors` | live base reload judged against the configured base; run file keeps `base` |
| TG v1.21.0 | TG run-wide increment, parity with CLI 2.37.0 | Adapted | `tg/config.py:45`, `tg/handlers.py:2429` | `tests/test_wolfbet_sync_e10.py::test_tg_init_run_creates_run_at_first_session` | WolfBet TG lacked it; added by request following v1.21.0 |

## Known gaps

Found by the cross-service regression suite (`tests/test_wolfbet_sync_e8_regression.py`); recorded, not fixed here.

- CLI user pause (SIGUSR1 / `p`) does not freeze session uptime; the regression test is a strict xfail.
