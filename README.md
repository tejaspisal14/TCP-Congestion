# TCP Reno, CUBIC and BBR: Reproducible Experiment Package

This package is the experiment artifact accompanying the revised manuscript draft. It collects measurements from a real Linux TCP stack in a Mininet emulation testbed. It does **not** generate synthetic measurements. Run it on the target Linux environment, then analyze the resulting CSV to generate the paper's tables and figures.

## Important limitations

- The experiment runner requires Linux, root privileges, Mininet, Open vSwitch, `iperf3`, and `ss` from `iproute2`. It is not a native Windows script. Use a dedicated Ubuntu VM or Linux machine with nested network support.
- The available congestion-control algorithms depend on the host kernel. The script stops if Reno, CUBIC, or BBR is unavailable; do not silently replace missing algorithms.
- `TCP BBR` means the BBR implementation exposed by the installed Linux kernel. Record the exact kernel version and, where possible, identify the BBR generation. This package does not switch kernels or assume every system's `bbr` means BBRv3.
- `ss` RTT/cwnd values are sampled during the test and can be missing if the OS's `ss` output or timing does not expose them. Missing data are left blank, not changed to zero.
- The tested network uses one shared bottleneck between two Open vSwitches. Mininet is an emulation environment, not a substitute for validating conclusions on a production Internet path.
- The manuscript draft intentionally has no made-up numerical results. Do not submit it until the testbed has been run, outputs reviewed, and the results/discussion/conclusion completed.

## 1. Install on Ubuntu Linux

Use Ubuntu 22.04/24.04 on bare metal or a VM where Linux network namespaces and Open vSwitch work. An ordinary Windows PowerShell session is not sufficient.

```bash
sudo apt update
sudo apt install -y mininet openvswitch-switch iperf3 iproute2 iputils-ping python3-pip python3-venv git
sudo systemctl enable --now openvswitch-switch
# Mininet is imported from the system Python installed by apt.
/usr/bin/python3 -c "import mininet; print('Mininet import OK')"
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Check the environment:

```bash
uname -r
iperf3 --version
sysctl net.ipv4.tcp_available_congestion_control
sudo mn --test pingall
```

The final check should complete successfully. The congestion-control list must contain `reno`, `cubic`, and `bbr`. If BBR is not listed, check the kernel/module configuration (for example, `sudo modprobe tcp_bbr`) and reboot only if required by your system's module/kernel setup. Do not proceed until all three algorithms are available.

## 2. Smoke test

This is a toolchain check, not a paper result. It runs short transfers and one repetition per condition:

```bash
sudo /usr/bin/python3 src/run_experiments.py --quick --output-dir results-smoke
.venv/bin/python src/analyze_results.py --input results-smoke/runs.csv --output-dir results-smoke/analysis
```

If any scenario fails, read the error and `mininet`/Open vSwitch logs before starting the full study. A smoke test with 5-second transfers is not suitable for reported research findings.

## 3. Run the full experiment

The default run uses five independent repetitions, 30-second iperf3 transfers, and a 3-second omitted warm-up. It performs the following one-factor-at-a-time scenarios:

| Scenario | Bottleneck bandwidth | Link delay setting | Injected random loss | Queue limit |
|---|---:|---:|---:|---:|
| baseline | 20 Mbit/s | 10 ms | 0% | 100 packets |
| low_bandwidth | 5 Mbit/s | 10 ms | 0% | 100 packets |
| high_delay | 20 Mbit/s | 50 ms | 0% | 100 packets |
| random_loss_0_5pct | 20 Mbit/s | 10 ms | 0.5% | 100 packets |
| small_queue | 20 Mbit/s | 10 ms | 0% | 20 packets |

The delay parameter is the configured Mininet link delay; because both directions use the link's traffic-control interfaces, the paper should describe this as the per-direction link setting and report observed RTT separately. Verify the exact qdisc parameters on the system used for the final experiments. The `loss_percent` is the configured impairment, not the observed loss rate.

```bash
sudo /usr/bin/python3 src/run_experiments.py --output-dir results
.venv/bin/python src/analyze_results.py --input results/runs.csv --output-dir results/analysis
```

Expected run duration depends on CPU and Mininet overhead. The full suite runs a single-flow test for each algorithm/scenario/repetition plus simultaneous Reno/CUBIC/BBR mixed-flow trials at baseline. You may run a smaller initial matrix, but report all settings transparently and do not combine smoke-test and full-test results.

To run only selected scenarios:

```bash
sudo /usr/bin/python3 src/run_experiments.py --scenarios baseline high_delay --repetitions 5 --duration 30 --output-dir results-selected
```

To omit fairness trials (not recommended if fairness is a paper claim):

```bash
sudo /usr/bin/python3 src/run_experiments.py --skip-fairness --output-dir results-no-fairness
```

## 4. Output files

- `results/runs.csv`: one row per flow with scenario settings, throughput, retransmissions, sampled RTT/cwnd, kernel version, and timestamp.
- `results/raw/*.json`: iperf3 JSON for each flow.
- `results/environment.json`: OS/kernel/tool and run configuration metadata.
- `results/analysis/summary_by_scenario_algorithm.csv`: averages, standard deviations, sample counts and 95% confidence intervals.
- `results/analysis/fairness_by_run.csv`: Jain fairness index and aggregate throughput for each mixed-flow repetition.
- `results/analysis/*.png`: plots suitable for inclusion in a manuscript after visual inspection.
- `results/analysis/RESULTS_REPORT.md`: autogenerated descriptive summary from the measured CSV.

Keep the raw JSON, CSV, environment metadata, and exact source code used for the final paper. Do not hand-edit the raw measurement files. Each fresh run backs up an existing `runs.csv` before starting.

## 5. Methodological reporting notes

1. Specify the exact OS/kernel and the congestion-control implementation reported by `sysctl`.
2. Keep CPU load, virtualization settings, offload settings and qdisc configuration stable between comparable runs. Record deviations.
3. Run the experiment on an otherwise idle machine; do not run several independent experiments simultaneously.
4. Check raw iperf3 JSON and logs for failed or incomplete runs. Exclude a failed run only with an explicit, documented rule.
5. Treat retransmissions as a separate metric from the configured packet-loss rate.
6. A blank RTT/cwnd sample means the sampler did not record it. Do not coerce blank data to zero.
7. Report the measurement count and uncertainty. Five repetitions are a starting point, not a guarantee of sufficient statistical power.
8. Results from these emulated conditions apply to this testbed and parameter range; they do not prove that one algorithm is universally superior.

## 6. Reproducing the manuscript

After the full suite completes, inspect `results/analysis/RESULTS_REPORT.md`, the CSV summaries and plots. Transfer only measured results into the manuscript. Update the abstract, results/discussion, conclusion, and limitations. The manuscript contains explicit insertion notes; all of them must be removed before conference submission. Conference-specific formatting cannot be finalized until the target venue and its page limit/template are known.
