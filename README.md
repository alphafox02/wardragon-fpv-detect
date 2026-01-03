# WarDragon FPV Energy Scan

This repo contains the FPV energy scan workflow used by WarDragon. It scans known FPV center
frequencies with GNURadio + gr-inspector and can optionally confirm detections via `suscli fpvdet`.
Alerts are published over ZMQ in a message format compatible with the WarDragon ingestion pipeline.

Note: the `suscli fpvdet` detector plugin is **not** open source and is not included in this repo.
WarDragon kits ship with it installed; other systems need their own licensed build.

## Script

- `scripts/fpv_energy_scan.py`

## Requirements

- GNU Radio 3.10.x
- gr-inspector (GNURadio OOT module, installed system-wide)
- gr-osmosdr + SoapySDR (Pluto supported)
- `suscli` with fpvdet plugin (optional; required for confirm step)
- A calibrated `suscli` profile (example: `fpv58_race_2m`)

## ZMQ endpoints

Defaults (override with env vars or CLI flags):

- FPV XPUB: `FPV_ZMQ_ENDPOINT=tcp://127.0.0.1:4226`
- WarDragon monitor GPS (optional): `WARD_MON_ZMQ=tcp://127.0.0.1:4225`

The FPV publisher uses XPUB and emits a JSON list of message objects matching the DJI receiver
format (Basic ID, Location/Vector Message if GPS available, Self-ID Message, Frequency Message,
Signal Info).

## Environment overrides

- `FPV_ZMQ_ENDPOINT`: XPUB endpoint for FPV alerts
- `WARD_MON_ZMQ`: monitor GPS endpoint
- `WARD_MON_RECV_TIMEOUT_MS`: monitor recv timeout

## Running

```bash
python3 scripts/fpv_energy_scan.py
```

This workflow is intended for the WarDragon kit.

Service wrapper (stops AntSDR DJI, runs scan, then restarts DJI on exit):

```bash
scripts/fpv_energy_service.sh -z --zmq-endpoint tcp://127.0.0.1:4226
```

Optional systemd unit:

```bash
sudo cp scripts/fpv-receiver.service /etc/systemd/system/fpv-receiver.service
sudo editor /etc/systemd/system/fpv-receiver.service
sudo systemctl daemon-reload
sudo systemctl start fpv-receiver
```

Enable ZMQ output and custom endpoints:

```bash
python3 scripts/fpv_energy_scan.py -z --zmq-endpoint tcp://127.0.0.1:4226 \
  --monitor-endpoint tcp://127.0.0.1:4225
```

Debug output:

```bash
python3 scripts/fpv_energy_scan.py -d
```

Use a different SDR (gr-osmosdr args):

```bash
python3 scripts/fpv_energy_scan.py --osmosdr-args "driver=soapy,soapy=hackrf" \
  --samp-rate 8e6 --bandwidth 8e6 --gain 20
```

## Notes

- The script stops GNURadio before running `suscli` confirm, then restarts it.
- If `suscli fpvdet` is unavailable, confirm is skipped and only energy alerts are published.
- If Soapy/Pluto buffer errors occur, the script retries with backoff.
- Calibration/profile settings must match `suscli` confirm parameters (bandwidth, dt, Q, rate).
- A startup warm-up sweep is used to estimate a global noise floor when fixed thresholding is
  enabled. If a strong transmitter is on during warm-up, the threshold may be biased high.
- Edit constants at the top of the script to change center lists, dwell/settle times,
  thresholds, and confirm profile settings.

## How Detection Works (Summary)

1) The script tunes across known FPV center frequencies and runs the gr-inspector energy
   detector at each center for a short dwell.
2) Any detected signal wider than `MIN_BW_HZ` is treated as a candidate.
3) Candidates are confirmed with `suscli fpvdet`, which estimates PAL/NTSC confidence.
4) If ZMQ is enabled, the script publishes both an energy alert and a confirm alert.

## Tuning Guide

Key parameters near the top of `scripts/fpv_energy_scan.py`:

- `MIN_BW_HZ`: Minimum detected bandwidth to accept. Typical analog FPV is ~6–8 MHz.
  Use 4e6 for fewer false positives; use 2e6 if the detector underestimates bandwidth.
- `SETTLE_S`: Retune settle time. Too low can cause stale samples.
- `DWELL_S`: How long to observe each center. Longer increases detection but slows the sweep.
- `AUTO_THRESHOLD`: If True, gr-inspector estimates the noise floor every dwell.
  If False, a fixed threshold is used.
- `THRESHOLD_DB`: Fixed threshold (used when auto is off).
- `THRESHOLD_OFFSET_DB`: When auto is off, a warm-up sweep computes a global noise median
  and this offset is added (e.g., noise -91 dB + offset 6 dB = threshold -85 dB).

Suggested defaults for outdoor use:
- `MIN_BW_HZ = 4e6`
- `THRESHOLD_OFFSET_DB = 6.0`

If you miss weak signals:
- Lower `THRESHOLD_OFFSET_DB` by 1–2 dB, or
- Drop `MIN_BW_HZ` to 2e6.

If you see too many false positives:
- Raise `THRESHOLD_OFFSET_DB` by 1–2 dB, or
- Increase `MIN_BW_HZ` toward 6e6.
