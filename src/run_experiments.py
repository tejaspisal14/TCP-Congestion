#!/usr/bin/env python3
"""Reproducible Linux/Mininet TCP Reno, CUBIC and BBR experiment runner.

Requires root and a working Mininet installation. The program writes one CSV row
per measured flow, raw iperf3 JSON files, and environment metadata. It does not
invent or simulate experimental measurements.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import platform
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from statistics import mean, median
from typing import Any

try:
    from mininet.net import Mininet
    from mininet.node import OVSSwitch
    from mininet.link import TCLink
    from mininet.log import setLogLevel, info, error
except ImportError as exc:  # friendly failure on non-Mininet systems
    print(
        "ERROR: Mininet is not installed or is not importable. Run this script "
        "on a Linux machine with Mininet installed (see README.md).",
        file=sys.stderr,
    )
    raise SystemExit(2) from exc

ALGORITHMS = {"Reno": "reno", "CUBIC": "cubic", "BBR": "bbr"}

# One-factor-at-a-time scenario matrix. Values are proposed experimental settings,
# not claims about measured network performance.
SCENARIOS: dict[str, dict[str, float | int]] = {
    "baseline": {"bw_mbps": 20.0, "delay_ms": 10.0, "loss_percent": 0.0, "queue_packets": 100},
    "low_bandwidth": {"bw_mbps": 5.0, "delay_ms": 10.0, "loss_percent": 0.0, "queue_packets": 100},
    "high_delay": {"bw_mbps": 20.0, "delay_ms": 50.0, "loss_percent": 0.0, "queue_packets": 100},
    "random_loss_0_5pct": {"bw_mbps": 20.0, "delay_ms": 10.0, "loss_percent": 0.5, "queue_packets": 100},
    "small_queue": {"bw_mbps": 20.0, "delay_ms": 10.0, "loss_percent": 0.0, "queue_packets": 20},
}

CSV_FIELDS = [
    "run_id", "flow_id", "test_type", "scenario", "algorithm", "repetition",
    "bottleneck_bw_mbps", "one_way_delay_ms", "configured_loss_percent", "queue_packets",
    "duration_s", "warmup_s", "throughput_mbps", "sender_throughput_mbps", "retransmits",
    "rtt_mean_ms", "rtt_median_ms", "rtt_p95_ms", "cwnd_median_segments", "cwnd_max_segments",
    "kernel_release", "started_at_utc", "iperf3_version",
]


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def command_output(args: list[str]) -> str:
    try:
        result = subprocess.run(args, check=False, capture_output=True, text=True, timeout=10)
        return (result.stdout or result.stderr).strip()
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"


def require_root() -> None:
    if os.geteuid() != 0:
        raise SystemExit("ERROR: run with sudo: sudo python3 src/run_experiments.py")
    missing = [name for name in ("iperf3", "ss", "ovs-vsctl") if shutil.which(name) is None]
    if missing:
        raise SystemExit("ERROR: missing required executable(s): " + ", ".join(missing))


def make_network(config: dict[str, float | int]) -> Mininet:
    """Create a six-host dumbbell with one controllable switch-to-switch link."""
    net = Mininet(controller=None, switch=OVSSwitch, link=TCLink, build=False, autoSetMacs=True)
    left = net.addSwitch("s1", failMode="standalone")
    right = net.addSwitch("s2", failMode="standalone")
    senders = [net.addHost(f"h{i}") for i in (1, 2, 3)]
    receivers = [net.addHost(f"h{i}") for i in (4, 5, 6)]

    # Access links are deliberately over-provisioned relative to the bottleneck.
    for host in senders:
        net.addLink(host, left, cls=TCLink, bw=100.0, delay="1ms", max_queue_size=100)
    for host in receivers:
        net.addLink(host, right, cls=TCLink, bw=100.0, delay="1ms", max_queue_size=100)
    net.addLink(
        left, right, cls=TCLink,
        bw=float(config["bw_mbps"]),
        delay=f"{float(config['delay_ms'])}ms",
        loss=float(config["loss_percent"]),
        max_queue_size=int(config["queue_packets"]),
    )
    net.build()
    net.start()
    # Keep OVS forwarding without a controller.
    for sw in (left, right):
        sw.cmd(f"ovs-vsctl set-fail-mode {sw.name} standalone >/dev/null 2>&1 || true")
    time.sleep(0.5)
    return net


def verify_algorithms(net: Mininet) -> dict[str, str]:
    available: dict[str, str] = {}
    for host in net.hosts:
        value = host.cmd("sysctl -n net.ipv4.tcp_available_congestion_control 2>/dev/null").strip()
        available[host.name] = value
    for name, cc in ALGORITHMS.items():
        if cc not in available.get("h1", "").split():
            raise RuntimeError(
                f"TCP congestion-control algorithm '{cc}' (requested as {name}) is not "
                f"available in this Linux kernel. Available on h1: {available.get('h1', '')}. "
                "Check the kernel/module configuration before running the study."
            )
    return available


def set_congestion_control(host: Any, algorithm: str) -> None:
    cc = ALGORITHMS[algorithm]
    result = host.cmd(f"sysctl -qw net.ipv4.tcp_congestion_control={cc} 2>&1; echo __RC:$?")
    match = re.search(r"__RC:(\d+)", result)
    if not match or int(match.group(1)) != 0:
        raise RuntimeError(f"Could not set {host.name} TCP congestion control to {cc}: {result.strip()}")
    actual = host.cmd("sysctl -n net.ipv4.tcp_congestion_control 2>/dev/null").strip()
    if actual != cc:
        raise RuntimeError(f"Kernel reports {actual!r} instead of requested {cc!r} on {host.name}")


def launch_server(host: Any, port: int) -> Any:
    proc = host.popen(
        ["iperf3", "-s", "-1", "-p", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(0.25)
    if proc.poll() is not None:
        stderr = proc.stderr.read() if proc.stderr else ""
        raise RuntimeError(f"iperf3 server failed on {host.name}:{port}: {stderr}")
    return proc


def monitor_tcp_socket(host: Any, destination_ip: str, port: int, stop_event: threading.Event, values: dict[str, list[float]]) -> None:
    """Sample RTT and cwnd from ss while the sender's TCP connection is active."""
    rtt_re = re.compile(r"\brtt:([0-9.]+)/([0-9.]+)")
    cwnd_re = re.compile(r"\bcwnd:(\d+)")
    while not stop_event.is_set():
        try:
            output = host.cmd(f"ss -tin dst {destination_ip}:{port} 2>/dev/null")
            rtt_match = rtt_re.search(output)
            if rtt_match:
                values["rtt_ms"].append(float(rtt_match.group(1)))
            cwnd_match = cwnd_re.search(output)
            if cwnd_match:
                values["cwnd_segments"].append(float(cwnd_match.group(1)))
        except Exception:
            # Measurement sampling is auxiliary; iperf3 remains the throughput source.
            pass
        stop_event.wait(0.35)


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def parse_iperf_json(text: str) -> tuple[dict[str, Any], float | None, float | None, int | None]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"iperf3 did not produce valid JSON: {text[-500:]}") from exc
    if "error" in payload:
        raise RuntimeError(f"iperf3 reported an error: {payload['error']}")
    end = payload.get("end", {})
    sent = end.get("sum_sent") or end.get("sum") or {}
    received = end.get("sum_received") or end.get("sum") or {}
    recv_bps = received.get("bits_per_second")
    sent_bps = sent.get("bits_per_second")
    retrans = sent.get("retransmits")
    # Some iperf3 builds expose retransmission counts on another end-summary key.
    if retrans is None:
        retrans = end.get("sum_sent", {}).get("retransmits")
    return payload, (float(recv_bps) / 1e6 if recv_bps is not None else None), (float(sent_bps) / 1e6 if sent_bps is not None else None), (int(retrans) if retrans is not None else None)


def run_flow(
    *, net: Mininet, sender: Any, receiver: Any, port: int, algorithm: str,
    run_id: str, flow_id: str, test_type: str, scenario: str, repetition: int,
    config: dict[str, float | int], duration_s: int, warmup_s: int, out_dir: Path,
    metadata: dict[str, str],
) -> dict[str, Any]:
    set_congestion_control(sender, algorithm)
    server = launch_server(receiver, port)
    stop_event = threading.Event()
    sample_values: dict[str, list[float]] = {"rtt_ms": [], "cwnd_segments": []}
    monitor = threading.Thread(
        target=monitor_tcp_socket,
        args=(sender, receiver.IP(), port, stop_event, sample_values),
        daemon=True,
    )
    monitor.start()
    start_at = utc_now()
    client_cmd = [
        "iperf3", "-c", receiver.IP(), "-p", str(port), "-t", str(duration_s),
        "-O", str(warmup_s), "-J", "--get-server-output",
    ]
    client = sender.popen(client_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        stdout, stderr = client.communicate(timeout=duration_s + 30)
    except subprocess.TimeoutExpired:
        client.kill()
        stdout, stderr = client.communicate()
        raise RuntimeError(f"iperf3 client timed out for {run_id}/{flow_id}: {stderr[-300:]}")
    finally:
        stop_event.set()
        monitor.join(timeout=2)

    if client.returncode != 0:
        raise RuntimeError(f"iperf3 client failed for {run_id}/{flow_id}: {stderr[-1000:]}")
    payload, throughput, sender_throughput, retransmits = parse_iperf_json(stdout)
    raw_dir = out_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_file = raw_dir / f"{run_id}_{flow_id}.json"
    raw_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    rtts = sample_values["rtt_ms"]
    cwnds = sample_values["cwnd_segments"]
    row: dict[str, Any] = {
        "run_id": run_id,
        "flow_id": flow_id,
        "test_type": test_type,
        "scenario": scenario,
        "algorithm": algorithm,
        "repetition": repetition,
        "bottleneck_bw_mbps": config["bw_mbps"],
        "one_way_delay_ms": config["delay_ms"],
        "configured_loss_percent": config["loss_percent"],
        "queue_packets": config["queue_packets"],
        "duration_s": duration_s,
        "warmup_s": warmup_s,
        "throughput_mbps": throughput,
        "sender_throughput_mbps": sender_throughput,
        "retransmits": retransmits,
        "rtt_mean_ms": mean(rtts) if rtts else None,
        "rtt_median_ms": median(rtts) if rtts else None,
        "rtt_p95_ms": percentile(rtts, 0.95),
        "cwnd_median_segments": median(cwnds) if cwnds else None,
        "cwnd_max_segments": max(cwnds) if cwnds else None,
        "kernel_release": platform.release(),
        "started_at_utc": start_at,
        "iperf3_version": metadata["iperf3_version"],
    }
    # Reap the one-shot server and include server-side error if it exited abnormally.
    try:
        _, server_err = server.communicate(timeout=3)
    except subprocess.TimeoutExpired:
        server.terminate()
        _, server_err = server.communicate(timeout=2)
    if server.returncode not in (0, None):
        raise RuntimeError(f"iperf3 server returned {server.returncode}: {(server_err or '')[-500:]}")
    return row


def append_row(csv_path: Path, row: dict[str, Any]) -> None:
    new_file = not csv_path.exists()
    with csv_path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        if new_file:
            writer.writeheader()
        writer.writerow(row)
        handle.flush()
        os.fsync(handle.fileno())


def run_fairness_trial(
    *, net: Mininet, repetition: int, config: dict[str, float | int], duration_s: int,
    warmup_s: int, out_dir: Path, metadata: dict[str, str], csv_path: Path,
) -> None:
    """Run one Reno, one CUBIC and one BBR flow at the same time over the bottleneck."""
    run_id = f"fairness_baseline_r{repetition:02d}_{uuid.uuid4().hex[:8]}"
    senders = [net.get("h1"), net.get("h2"), net.get("h3")]
    receivers = [net.get("h4"), net.get("h5"), net.get("h6")]
    algorithms = ["Reno", "CUBIC", "BBR"]
    ports = [5201, 5202, 5203]

    for sender, algo in zip(senders, algorithms):
        set_congestion_control(sender, algo)
    servers = [launch_server(receiver, port) for receiver, port in zip(receivers, ports)]
    stop_events = [threading.Event() for _ in senders]
    samples = [{"rtt_ms": [], "cwnd_segments": []} for _ in senders]
    threads = [
        threading.Thread(target=monitor_tcp_socket,
                         args=(sender, receiver.IP(), port, stop_event, sample), daemon=True)
        for sender, receiver, port, stop_event, sample in zip(senders, receivers, ports, stop_events, samples)
    ]
    for thread in threads:
        thread.start()
    started_at = utc_now()
    clients = []
    for sender, receiver, port in zip(senders, receivers, ports):
        clients.append(sender.popen(
            ["iperf3", "-c", receiver.IP(), "-p", str(port), "-t", str(duration_s),
             "-O", str(warmup_s), "-J", "--get-server-output"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ))
    try:
        outcomes = []
        for client in clients:
            try:
                stdout, stderr = client.communicate(timeout=duration_s + 30)
            except subprocess.TimeoutExpired:
                client.kill()
                stdout, stderr = client.communicate()
                raise RuntimeError(f"Fairness iperf3 client timed out: {stderr[-300:]}")
            if client.returncode != 0:
                raise RuntimeError(f"Fairness iperf3 client failed: {stderr[-1000:]}")
            outcomes.append(parse_iperf_json(stdout))
    finally:
        for event in stop_events:
            event.set()
        for thread in threads:
            thread.join(timeout=2)

    for server in servers:
        try:
            _, server_err = server.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            server.terminate()
            _, server_err = server.communicate(timeout=2)
        if server.returncode not in (0, None):
            raise RuntimeError(f"Fairness iperf3 server exited unexpectedly: {(server_err or '')[-500:]}")

    raw_dir = out_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    for index, (sender, receiver, algo, outcome, sample, port) in enumerate(
        zip(senders, receivers, algorithms, outcomes, samples, ports), start=1
    ):
        payload, throughput, sender_throughput, retransmits = outcome
        (raw_dir / f"{run_id}_flow{index}.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        rtts = sample["rtt_ms"]
        cwnds = sample["cwnd_segments"]
        row = {
            "run_id": run_id,
            "flow_id": f"flow{index}_{sender.name}_to_{receiver.name}",
            "test_type": "mixed_fairness",
            "scenario": "baseline_mixed_flows",
            "algorithm": algo,
            "repetition": repetition,
            "bottleneck_bw_mbps": config["bw_mbps"],
            "one_way_delay_ms": config["delay_ms"],
            "configured_loss_percent": config["loss_percent"],
            "queue_packets": config["queue_packets"],
            "duration_s": duration_s,
            "warmup_s": warmup_s,
            "throughput_mbps": throughput,
            "sender_throughput_mbps": sender_throughput,
            "retransmits": retransmits,
            "rtt_mean_ms": mean(rtts) if rtts else None,
            "rtt_median_ms": median(rtts) if rtts else None,
            "rtt_p95_ms": percentile(rtts, 0.95),
            "cwnd_median_segments": median(cwnds) if cwnds else None,
            "cwnd_max_segments": max(cwnds) if cwnds else None,
            "kernel_release": platform.release(),
            "started_at_utc": started_at,
            "iperf3_version": metadata["iperf3_version"],
        }
        append_row(csv_path, row)
        info(f"\n[RESULT] fairness rep={repetition} {algo}: {throughput if throughput is not None else 'NA'} Mbps\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="results", help="Directory for CSV, raw JSON and metadata")
    parser.add_argument("--duration", type=int, default=30, help="Duration per iperf3 transfer, seconds (default: 30)")
    parser.add_argument("--warmup", type=int, default=3, help="Initial seconds omitted from iperf3 measurement (default: 3)")
    parser.add_argument("--repetitions", type=int, default=5, help="Independent repetitions per condition (default: 5)")
    parser.add_argument("--scenarios", nargs="+", choices=sorted(SCENARIOS), default=list(SCENARIOS), help="Scenarios to run")
    parser.add_argument("--skip-fairness", action="store_true", help="Skip mixed Reno/CUBIC/BBR fairness experiments")
    parser.add_argument("--seed", type=int, default=20261020, help="Seed for randomized algorithm order (default: 20261020)")
    parser.add_argument("--quick", action="store_true", help="Smoke test: 5-second transfers and 1 repetition")
    parser.add_argument("--log-level", choices=["info", "debug", "warning", "error"], default="info")
    args = parser.parse_args()

    if args.quick:
        args.duration = 5
        args.warmup = 1
        args.repetitions = 1
    if args.duration < 3 or args.warmup < 0 or args.warmup >= args.duration or args.repetitions < 1:
        parser.error("duration must be >= 3, warmup must be >= 0 and less than duration, repetitions must be >= 1")
    require_root()
    setLogLevel(args.log_level)

    out_dir = Path(args.output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "runs.csv"
    if csv_path.exists():
        backup = csv_path.with_name(f"runs_existing_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
        shutil.copy2(csv_path, backup)
        csv_path.unlink()
        info(f"\nBacked up existing runs.csv to {backup}\n")

    iperf_version = command_output(["iperf3", "--version"]).splitlines()[0]
    metadata: dict[str, str] = {
        "created_at_utc": utc_now(),
        "kernel_release": platform.release(),
        "platform": platform.platform(),
        "python_version": sys.version.split()[0],
        "iperf3_version": iperf_version,
        "mininet_version": command_output(["mn", "--version"]) if shutil.which("mn") else "see installed Mininet package",
        "experiment_duration_s": str(args.duration),
        "warmup_s": str(args.warmup),
        "repetitions": str(args.repetitions),
        "scenarios": json.dumps(args.scenarios),
        "seed": str(args.seed),
        "algorithm_execution_order": json.dumps({}),
        "algorithm_mapping": json.dumps(ALGORITHMS),
        "note": "Rows are collected experimental measurements; no synthetic data are generated by this script.",
    }
    (out_dir / "environment.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    for scenario in args.scenarios:
        config = SCENARIOS[scenario]
        info(f"\n\n=== Scenario: {scenario} | config={config} ===\n")
        net = None
        try:
            net = make_network(config)
            available = verify_algorithms(net)
            metadata["available_congestion_control"] = json.dumps(available)
            (out_dir / "environment.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
            for repetition in range(1, args.repetitions + 1):
                algorithm_order = list(ALGORITHMS.keys())
                random.Random(args.seed + args.scenarios.index(scenario) * 1000 + repetition).shuffle(algorithm_order)
                order_map = json.loads(metadata.get("algorithm_execution_order", "{}"))
                order_map[f"{scenario}_rep_{repetition}"] = algorithm_order
                metadata["algorithm_execution_order"] = json.dumps(order_map)
                (out_dir / "environment.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
                for algorithm in algorithm_order:
                    run_id = f"{scenario}_{algorithm.lower()}_r{repetition:02d}_{uuid.uuid4().hex[:8]}"
                    info(f"\n--- {run_id} ---\n")
                    row = run_flow(
                        net=net, sender=net.get("h1"), receiver=net.get("h4"), port=5201,
                        algorithm=algorithm, run_id=run_id, flow_id="h1_to_h4", test_type="single_flow",
                        scenario=scenario, repetition=repetition, config=config, duration_s=args.duration,
                        warmup_s=args.warmup, out_dir=out_dir, metadata=metadata,
                    )
                    append_row(csv_path, row)
                    info(f"\n[RESULT] {scenario} {algorithm}: {row['throughput_mbps']} Mbps, retransmits={row['retransmits']}\n")
            if not args.skip_fairness and scenario == "baseline":
                for repetition in range(1, args.repetitions + 1):
                    info(f"\n--- mixed-flow fairness repetition {repetition} ---\n")
                    run_fairness_trial(
                        net=net, repetition=repetition, config=config, duration_s=args.duration,
                        warmup_s=args.warmup, out_dir=out_dir, metadata=metadata, csv_path=csv_path,
                    )
        finally:
            if net is not None:
                net.stop()
    print(f"\nExperiment run complete. Raw measurements: {csv_path}")
    print("Next: python3 src/analyze_results.py --input results/runs.csv --output-dir results/analysis")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted. Rows already flushed to CSV are retained.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
