#!/usr/bin/env python3
# Copyright 2025-2026 CEMAXECUTER LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import gc
import json
import os
import statistics
import subprocess
import time
from typing import Optional, Tuple

import pmt
import zmq
from gnuradio import gr, blocks
from gnuradio.fft import window
import osmosdr
try:
    from gnuradio import inspector
except ImportError:
    from gnuradio import inspector  # noqa: F401

# Standard FPV lists + extra 5.9 GHz centers to catch odd VTX offsets.
RACE_BANDS_ALL_MHZ = [
    # A
    5865, 5845, 5825, 5805, 5785, 5765, 5745, 5725,
    # B
    5733, 5752, 5771, 5790, 5809, 5828, 5847, 5866,
    # E
    5705, 5685, 5665, 5645, 5885, 5905, 5925, 5945,
    # F
    5740, 5760, 5780, 5800, 5820, 5840, 5860, 5880,
    # R (Race)
    5658, 5695, 5732, 5769, 5806, 5843, 5880, 5917,
    # L
    5333, 5373, 5413, 5453, 5493, 5533, 5573, 5613,
    # X
    4990, 5020, 5050, 5080, 5110, 5140, 5170, 5200,
]

EXTRA_59_MHZ = [5935, 5940, 5943, 5950]
ALL_CENTERS_MHZ = sorted(set(RACE_BANDS_ALL_MHZ + EXTRA_59_MHZ))

PLUTO_URI = "ant.local"
SAMP_RATE = 8e6
BANDWIDTH = 8e6
GAIN = 50
FFT_LEN = 4096

# Signal detector params
AUTO_THRESHOLD = False
SENSITIVITY = 0.6
THRESHOLD_DB = -90.0
AVERAGE = 0.5
QUANTIZATION = 0.05
MIN_BW_HZ = 4.0e6

SETTLE_S = 0.12
DWELL_S = 0.4
WARMUP_SWEEPS = 1
THRESHOLD_OFFSET_DB = 6.0

# suscli confirm settings (match your calibrated profile)
SUSCLI_BIN = "suscli"
SUSCLI_PROFILE = "fpv58_race_2m"
SUSCLI_BANDWIDTH = "1.8e6"
SUSCLI_DT = "0.1"
SUSCLI_Q = "10"
CONFIRM_SECONDS = 5
COOLDOWN_S = 3.0
REOPEN_RETRIES = 5
REOPEN_DELAY_S = 2.0

# ZMQ publish settings (XPUB)
FPV_ZMQ_ENDPOINT = os.getenv("FPV_ZMQ_ENDPOINT", "tcp://127.0.0.1:4226")
MON_ZMQ_ENDPOINT = os.getenv("WARD_MON_ZMQ", "tcp://127.0.0.1:4225")
MON_ZMQ_RECV_TIMEOUT_MS = int(os.getenv("WARD_MON_RECV_TIMEOUT_MS", "50"))
ALERT_ID_PREFIX = "fpv-alert"

_last_sensor_gps: Optional[Tuple[float, float, float]] = None
_confirm_disabled_reason: Optional[str] = None


def _disable_confirm(reason: str) -> None:
    global _confirm_disabled_reason
    if _confirm_disabled_reason is None:
        _confirm_disabled_reason = reason
        print(f"warning: suscli confirm disabled ({reason})")


class InspectorScan(gr.top_block):
    def __init__(self, threshold_db, source_args, samp_rate, bandwidth, gain):
        super().__init__()

        self.src = osmosdr.source(source_args)
        self.src.set_sample_rate(samp_rate)
        self.src.set_center_freq(ALL_CENTERS_MHZ[0] * 1e6)
        self.src.set_bandwidth(bandwidth)
        self.src.set_gain(gain)

        self.detector = inspector.signal_detector_cvf(
            samp_rate,
            FFT_LEN,
            window.WIN_BLACKMAN_hARRIS,
            threshold_db,
            SENSITIVITY,
            AUTO_THRESHOLD,
            AVERAGE,
            QUANTIZATION,
            MIN_BW_HZ,
            "",
        )

        self.null = blocks.null_sink(gr.sizeof_float * FFT_LEN)
        self.msg_dbg = blocks.message_debug()
        self.probe = blocks.probe_signal_vf(FFT_LEN)

        self.connect(self.src, self.detector)
        self.connect((self.detector, 0), self.null)
        self.connect((self.detector, 0), self.probe)
        self.msg_connect((self.detector, "map_out"), (self.msg_dbg, "store"))

    def set_center(self, hz):
        self.src.set_center_freq(hz)

    def get_latest_map(self):
        count = self.msg_dbg.num_messages()
        if count == 0:
            return None
        msg = self.msg_dbg.get_message(count - 1)
        return msg

    def num_messages(self):
        return self.msg_dbg.num_messages()

    def get_message(self, idx):
        return self.msg_dbg.get_message(idx)

    def get_latest_spectrum(self):
        return self.probe.level()


def parse_rf_map(msg, center_hz):
    signals = []
    if msg is None:
        return signals
    for i in range(pmt.length(msg)):
        row = pmt.vector_ref(msg, i)
        freq_off = pmt.f32vector_ref(row, 0)
        bw = pmt.f32vector_ref(row, 1)
        abs_hz = center_hz + freq_off
        if bw >= MIN_BW_HZ:
            signals.append((abs_hz, bw))
    return signals


def is_valid_latlon(lat, lon):
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return False
    return -90.0 <= float(lat) <= 90.0 and -180.0 <= float(lon) <= 180.0


def setup_monitor_sub(endpoint):
    try:
        ctx = zmq.Context.instance()
        sub = ctx.socket(zmq.SUB)
        sub.setsockopt(zmq.SUBSCRIBE, b"")
        sub.setsockopt(zmq.RCVTIMEO, MON_ZMQ_RECV_TIMEOUT_MS)
        sub.connect(endpoint)
        return sub
    except Exception:
        return None


def poll_monitor_for_gps(sub_sock):
    global _last_sensor_gps
    if sub_sock is None:
        return
    try:
        msg = sub_sock.recv_string(flags=zmq.NOBLOCK)
    except zmq.Again:
        return
    except Exception:
        return

    try:
        payload = json.loads(msg)
    except json.JSONDecodeError:
        return

    # Expect WarDragon monitor JSON to have gps info at top level.
    lat = payload.get("lat")
    lon = payload.get("lon")
    alt = payload.get("alt", 0.0)
    if is_valid_latlon(lat, lon):
        _last_sensor_gps = (float(lat), float(lon), float(alt))


def build_alert_messages(center_hz, bandwidth_hz, pal, ntsc, source):
    message_list = []
    freq_mhz = center_hz / 1e6
    alert_id = f"{ALERT_ID_PREFIX}-{freq_mhz:.3f}MHz"

    basic_id = {
        "Basic ID": {
            "id_type": "Serial Number (ANSI/CTA-2063-A)",
            "id": alert_id,
            "description": "FPV Signal",
        }
    }
    message_list.append(basic_id)

    if _last_sensor_gps is not None:
        lat, lon, alt = _last_sensor_gps
        location = {
            "Location/Vector Message": {
                "latitude": lat,
                "longitude": lon,
                "geodetic_altitude": alt,
                "height_agl": 0.0,
                "speed": 0.0,
                "vert_speed": 0.0,
            }
        }
        message_list.append(location)

    self_id = {
        "Self-ID Message": {
            "text": f"FPV alert ({source})",
        }
    }
    message_list.append(self_id)

    freq_msg = {
        "Frequency Message": {
            "frequency": center_hz,
        }
    }
    message_list.append(freq_msg)

    signal_info = {
        "Signal Info": {
            "source": source,
            "center_hz": center_hz,
            "bandwidth_hz": bandwidth_hz,
            "pal_conf": pal,
            "ntsc_conf": ntsc,
        }
    }
    message_list.append(signal_info)

    return message_list


def publish_alert(pub_socket, center_hz, bandwidth_hz, pal, ntsc, source):
    message_list = build_alert_messages(center_hz, bandwidth_hz, pal, ntsc, source)
    try:
        pub_socket.send_string(json.dumps(message_list))
    except Exception:
        pass


def run_confirm(center_hz):
    if _confirm_disabled_reason is not None:
        return None, None
    cmd = [
        SUSCLI_BIN,
        "fpvdet",
        f"--profile={SUSCLI_PROFILE}",
        f"--frequency={center_hz}",
        f"--bandwidth={SUSCLI_BANDWIDTH}",
        f"--dt={SUSCLI_DT}",
        f"--q={SUSCLI_Q}",
        "--formatter=json",
    ]

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=CONFIRM_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        _disable_confirm(f"{SUSCLI_BIN} not found in PATH")
        return None, None
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or b""
        if isinstance(output, bytes):
            output = output.decode(errors="replace")
    else:
        output = proc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode(errors="replace")
        stderr_text = proc.stderr or ""
        if isinstance(stderr_text, bytes):
            stderr_text = stderr_text.decode(errors="replace")
        combined = f"{output}\n{stderr_text}"
        if proc.returncode != 0:
            if "Unknown command" in combined or "unknown command" in combined:
                _disable_confirm("suscli fpvdet command not available")
                return None, None

    max_pal = 0.0
    max_ntsc = 0.0
    skip_lines = 2
    for line in output.splitlines():
        if not line.startswith("{"):
            continue
        if skip_lines > 0:
            skip_lines -= 1
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        sig = data.get("signal", {})
        pal = float(sig.get("pal", 0.0))
        ntsc = float(sig.get("ntsc", 0.0))
        max_pal = max(max_pal, pal)
        max_ntsc = max(max_ntsc, ntsc)

    return max_pal, max_ntsc


def start_tb_with_retry(threshold_db, source_args, samp_rate, bandwidth, gain):
    for attempt in range(1, REOPEN_RETRIES + 1):
        tb = InspectorScan(threshold_db, source_args, samp_rate, bandwidth, gain)
        try:
            tb.start()
            return tb
        except RuntimeError as exc:
            tb.stop()
            tb.wait()
            print(f"warning: failed to open SDR (attempt {attempt}): {exc}")
            time.sleep(REOPEN_DELAY_S)
    raise RuntimeError("failed to reopen SDR after retries")


def warmup_threshold(tb):
    medians = []
    for _ in range(WARMUP_SWEEPS):
        for mhz in ALL_CENTERS_MHZ:
            center_hz = mhz * 1e6
            tb.set_center(center_hz)
            time.sleep(SETTLE_S)
            time.sleep(DWELL_S)
            spectrum = tb.get_latest_spectrum()
            if spectrum:
                try:
                    medians.append(statistics.median(spectrum))
                except statistics.StatisticsError:
                    continue
    if not medians:
        return THRESHOLD_DB
    return statistics.median(medians) + THRESHOLD_OFFSET_DB


def parse_args():
    parser = argparse.ArgumentParser(
        description="FPV energy scan with optional suscli confirmation and ZMQ publish."
    )
    parser.add_argument(
        "-z",
        "--zmq",
        action="store_true",
        help="Enable ZMQ XPUB output (default: off).",
    )
    parser.add_argument(
        "--zmq-endpoint",
        default=FPV_ZMQ_ENDPOINT,
        help=f"FPV XPUB endpoint (default: {FPV_ZMQ_ENDPOINT})",
    )
    parser.add_argument(
        "--monitor-endpoint",
        default=MON_ZMQ_ENDPOINT,
        help=f"WarDragon monitor ZMQ endpoint (default: {MON_ZMQ_ENDPOINT})",
    )
    parser.add_argument(
        "-d",
        "--debug",
        action="store_true",
        help="Enable extra debug output.",
    )
    parser.add_argument(
        "--osmosdr-args",
        help="Override gr-osmosdr source args (e.g., 'plutosdr=ip:ant.local').",
    )
    parser.add_argument(
        "--pluto-uri",
        default=PLUTO_URI,
        help=(
            "Pluto address/URI when using default args "
            f"(default: {PLUTO_URI}, examples: ant.local, ip:ant.local, usb:0.1.5)"
        ),
    )
    parser.add_argument(
        "--samp-rate",
        type=float,
        default=SAMP_RATE,
        help=f"Sample rate in Hz (default: {SAMP_RATE}).",
    )
    parser.add_argument(
        "--bandwidth",
        type=float,
        default=BANDWIDTH,
        help=f"RF bandwidth in Hz (default: {BANDWIDTH}).",
    )
    parser.add_argument(
        "--gain",
        type=float,
        default=GAIN,
        help=f"RF gain (default: {GAIN}).",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    pub = None
    if args.zmq:
        ctx = zmq.Context.instance()
        pub = ctx.socket(zmq.XPUB)
        pub.setsockopt(zmq.XPUB_VERBOSE, True)
        pub.bind(args.zmq_endpoint)

    mon_sub = setup_monitor_sub(args.monitor_endpoint)

    threshold_db = THRESHOLD_DB
    if args.osmosdr_args:
        source_args = args.osmosdr_args
    else:
        source_args = f"soapy=driver=plutosdr,addr={args.pluto_uri}"

    tb = InspectorScan(threshold_db, source_args, args.samp_rate, args.bandwidth, args.gain)
    tb.start()
    if WARMUP_SWEEPS > 0 and not AUTO_THRESHOLD:
        threshold_db = warmup_threshold(tb)
        if args.debug:
            print(f"debug: warmup threshold={threshold_db:.2f} dB")
        tb.stop()
        tb.wait()
        tb = None
        gc.collect()
        time.sleep(COOLDOWN_S)
        tb = start_tb_with_retry(
            threshold_db,
            source_args,
            args.samp_rate,
            args.bandwidth,
            args.gain,
        )
    try:
        while True:
            for mhz in ALL_CENTERS_MHZ:
                center_hz = mhz * 1e6
                poll_monitor_for_gps(mon_sub)
                tb.set_center(center_hz)
                time.sleep(SETTLE_S)
                time.sleep(DWELL_S)
                msg = tb.get_latest_map()
                signals = parse_rf_map(msg, center_hz)
                if signals:
                    confirm_hz, confirm_bw = max(signals, key=lambda s: s[1])
                    sig_str = ", ".join(
                        f"{s[0]/1e6:.3f}MHz bw={s[1]/1e3:.1f}k"
                        for s in signals
                    )
                    print(f"center={mhz:.0f}MHz signals: {sig_str}")
                    if pub is not None:
                        publish_alert(pub, confirm_hz, confirm_bw, 0.0, 0.0, "energy")

                    # Confirm with suscli (release SDR first)
                    tb.stop()
                    tb.wait()
                    tb = None
                    gc.collect()
                    time.sleep(COOLDOWN_S)
                    pal, ntsc = run_confirm(confirm_hz)
                    if pal is None or ntsc is None:
                        print(
                            f"confirm center={confirm_hz/1e6:.3f}MHz skipped (suscli unavailable)"
                        )
                    else:
                        print(
                            f"confirm center={confirm_hz/1e6:.3f}MHz pal={pal:.1f} ntsc={ntsc:.1f}"
                        )
                        if pub is not None:
                            publish_alert(pub, confirm_hz, confirm_bw, pal, ntsc, "confirm")
                        if args.debug:
                            print(
                                f"debug: confirm center={confirm_hz/1e6:.3f}MHz "
                                f"bw={confirm_bw/1e6:.3f}MHz pal={pal:.1f} ntsc={ntsc:.1f}"
                            )
                    time.sleep(COOLDOWN_S)
                    tb = start_tb_with_retry(
                        threshold_db,
                        source_args,
                        args.samp_rate,
                        args.bandwidth,
                        args.gain,
                    )
                else:
                    print(f"center={mhz:.0f}MHz signals: none")
    except KeyboardInterrupt:
        pass
    finally:
        if tb is not None:
            tb.stop()
            tb.wait()


if __name__ == "__main__":
    main()
