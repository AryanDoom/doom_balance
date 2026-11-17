#!/usr/bin/env python3
"""run the project checks and collect their output for the report"""

import csv
import datetime
import html
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SDN_DIR = ROOT / 'sdn'
EVIDENCE_ROOT = SDN_DIR / 'evidence'

CHECKS = [
    ('rust_format', ['cargo', 'fmt', '--check'], False),
    ('rust_tests', ['cargo', 'test', '--all-targets'], False),
    ('rust_clippy', ['cargo', 'clippy', '--all-targets', '--', '-D', 'warnings'], False),
    ('mininet_baseline', ['run_tests.py'], True),
    ('scheduler_matrix', ['run_lb_matrix.py'], True),
    ('health_checks', ['run_health_test.py'], True),
    ('sdn_socket_integration', ['run_sdn_test.py'], True),
]


def run_check(name, command, needs_root, output_dir):
    log_path = output_dir / f'{name}.log'
    actual_command = list(command)
    if needs_root and os.geteuid() != 0:
        actual_command = ['sudo', sys.executable, str(SDN_DIR / command[0])]
    elif needs_root:
        actual_command = [sys.executable, str(SDN_DIR / command[0])]

    print(f'\n[{name}] running: {" ".join(actual_command)}')
    try:
        result = subprocess.run(
            actual_command,
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors='replace',
            check=False,
        )
        output = result.stdout
        return_code = result.returncode
    except OSError as error:
        output = f'could not start command: {error}\n'
        return_code = 127

    log_path.write_text(output, encoding='utf-8')
    print(output, end='' if output.endswith('\n') else '\n')
    status = 'PASS' if return_code == 0 else 'FAIL'
    print(f'[{name}] {status} (exit {return_code}; saved {log_path})')
    return {
        'name': name,
        'command': actual_command,
        'exit_code': return_code,
        'status': status,
        'log': log_path.name,
    }, output


def parse_results(outputs):
    scheduler_rows = []
    for line in outputs.get('scheduler_matrix', '').splitlines():
        match = re.search(
            r'^RESULT\s+(\S+)\s+algorithm=(\S+)\s+requests=(\d+)\s+'
            r'h3=(\d+)\s+h4=(\d+)\s+h3_share=([\d.]+)\s+'
            r'h4_share=([\d.]+)\s+expectation=(.+?)\s+'
            r'duration_s=([\d.]+)\s+requests_per_second=([\d.]+)$',
            line.strip(),
        )
        if match:
            scheduler_rows.append({
                'name': match.group(1),
                'algorithm': match.group(2),
                'requests': int(match.group(3)),
                'h3': int(match.group(4)),
                'h4': int(match.group(5)),
                'h3_share': float(match.group(6)),
                'h4_share': float(match.group(7)),
                'expectation': match.group(8),
                'duration_seconds': float(match.group(9)),
                'requests_per_second': float(match.group(10)),
            })

    markers = {}
    evidence_lines = []
    all_text = '\n'.join(outputs.values())
    for line in all_text.splitlines():
        if line.startswith((
            'PINGALL_LOSS_PERCENT:', 'TCP_h3:', 'TCP_h4:', 'LB_LISTEN:',
            'HEALTH_', 'SDN_', 'CONTROLLER_', 'RUST_BLOCK:', 'RUST_UNBLOCK:',
            'RUST_STATUS:', 'DIRECT_TCP_PASS', 'DIRECT_CONTROLLER_BLOCK_PASS',
            'SCHEDULER_MATRIX_PASS', 'HEALTH_TEST_PASS', 'SDN_TEST_PASS',
        )):
            evidence_lines.append(line.strip())
    for marker in (
        'DIRECT_TCP_PASS', 'SCHEDULER_MATRIX_PASS', 'HEALTH_TEST_PASS',
        'DIRECT_CONTROLLER_BLOCK_PASS', 'SDN_TEST_PASS', 'PINGALL_LOSS_PERCENT:',
    ):
        markers[marker] = marker in all_text

    return scheduler_rows, markers, evidence_lines


def make_svg(scheduler_rows):
    width = 1000
    height = 480
    left = 260
    right = 70
    top = 62
    row_gap = 74
    chart_width = width - left - right
    bars = []
    labels = []

    for index, row in enumerate(scheduler_rows):
        y = top + index * row_gap
        total = max(row['requests'], 1)
        h3_width = chart_width * row['h3'] / total
        h4_width = chart_width * row['h4'] / total
        labels.append(
            f'<text x="{left - 18}" y="{y + 25}" text-anchor="end" '
            f'font-size="20">{html.escape(row["algorithm"])}</text>'
        )
        bars.append(
            f'<rect x="{left}" y="{y}" width="{h3_width:.1f}" height="38" fill="#111" />'
            f'<rect x="{left + h3_width:.1f}" y="{y}" width="{h4_width:.1f}" height="38" fill="#888" />'
            f'<text x="{left + 8}" y="{y + 26}" fill="white" font-size="17">'
            f'h3 {row["h3"]}</text>'
            f'<text x="{left + h3_width + 8:.1f}" y="{y + 26}" fill="white" font-size="17">'
            f'h4 {row["h4"]}</text>'
        )

    chart_height = max(height, top + len(scheduler_rows) * row_gap + 35)
    body = ''.join(labels + bars)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{chart_height}" '
        f'viewBox="0 0 {width} {chart_height}">'
        '<rect width="100%" height="100%" fill="white" />'
        '<text x="40" y="35" font-size="24" font-family="Arial" font-weight="bold">'
        'Backend selections from the real Mininet run</text>'
        '<text x="40" y="65" font-size="15" font-family="Arial" fill="#555">'
        'Black = h3, grey = h4. Bar lengths are shares of requests within each algorithm.</text>'
        f'<g font-family="Arial">{body}</g></svg>'
    )


def make_report(output_dir, checks, scheduler_rows, markers, evidence_lines, environment):
    checks_html = ''.join(
        f'<tr><td>{html.escape(item["name"])}</td>'
        f'<td class="{item["status"].lower()}">{item["status"]}</td>'
        f'<td><a href="{html.escape(item["log"])}">full log</a></td></tr>'
        for item in checks
    )
    scheduler_html = ''.join(
        '<tr>'
        f'<td>{html.escape(row["algorithm"])}</td><td>{row["requests"]}</td>'
        f'<td>{row["h3"]}</td><td>{row["h4"]}</td>'
        f'<td>{row["h3_share"]:.0%} / {row["h4_share"]:.0%}</td>'
        f'<td>{row["duration_seconds"]:.3f} s</td>'
        f'<td>{row["requests_per_second"]:.2f}</td>'
        f'<td>{html.escape(row["expectation"])}</td></tr>'
        for row in scheduler_rows
    )
    marker_html = ''.join(
        f'<li class="{"pass" if passed else "fail"}">{html.escape(name)}: '
        f'{"observed" if passed else "not observed"}</li>'
        for name, passed in markers.items()
    )
    evidence_html = ''.join(
        f'<li><code>{html.escape(line)}</code></li>' for line in evidence_lines
    )
    environment_html = ''.join(
        f'<li><b>{html.escape(key)}:</b> {html.escape(str(value))}</li>'
        for key, value in environment.items()
    )
    svg_path = output_dir / 'scheduler_distribution.svg'
    svg_path.write_text(make_svg(scheduler_rows), encoding='utf-8')

    report = f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Load balancer evidence</title>
<style>
body {{ font: 16px Arial, sans-serif; color: #111; max-width: 1100px; margin: 40px auto; padding: 0 24px; }}
h1, h2 {{ border-bottom: 2px solid #111; padding-bottom: 8px; }}
table {{ border-collapse: collapse; width: 100%; margin: 18px 0 32px; }}
th, td {{ border: 1px solid #aaa; padding: 10px; text-align: left; }}
th {{ background: #eee; }} .pass {{ color: #111; }} .fail {{ color: #8a0000; }}
img {{ max-width: 100%; border: 1px solid #aaa; }}
.note {{ background: #f2f2f2; padding: 14px; }}
</style></head><body>
<h1>Rust TCP load balancer: test evidence</h1>
<p>Generated from command output on {html.escape(environment['time'])}. Open the linked logs to inspect the complete raw output.</p>
<h2>Run status</h2><table><thead><tr><th>Check</th><th>Status</th><th>Output</th></tr></thead>
<tbody>{checks_html}</tbody></table>
<h2>Scheduler results</h2>
<p>These counts are the actual accepted connections reported by the Mininet test. They show how each rule distributed this specific batch; they do not prove that one algorithm is always fastest.</p>
<img src="scheduler_distribution.svg" alt="Backend connection distribution chart">
<table><thead><tr><th>Algorithm</th><th>Requests</th><th>h3</th><th>h4</th><th>Share h3 / h4</th><th>Batch time</th><th>Requests / second</th><th>Test expectation</th></tr></thead>
<tbody>{scheduler_html or '<tr><td colspan="8">No scheduler RESULT lines were captured.</td></tr>'}</tbody></table>
<h2>Observed integration markers</h2><ul>{marker_html}</ul>
<h2>Connectivity, health, and SDN results</h2>
<ul>{evidence_html or '<li>No summary lines were captured; check the raw logs.</li>'}</ul>
<h2>Environment</h2><ul>{environment_html}</ul>
<p class="note"><b>How to read the algorithm results:</b> weighted round robin checks configured weights; IP hash checks repeatable client affinity (and can be uneven); random checks that both backends receive traffic in this sample; round robin and least connections are compared by accepted connection counts. Batch time and requests per second are rough measurements from one sequential client run in this Mininet setup. They are useful for a small project comparison, not a general claim that one algorithm is always fastest. Repeat runs and a defined workload would be needed for a stronger performance claim.</p>
</body></html>'''
    (output_dir / 'report.html').write_text(report, encoding='utf-8')


def capture_screenshot(output_dir):
    browser = None
    for name in ('chromium', 'chromium-browser', 'google-chrome', 'microsoft-edge', 'msedge'):
        browser = shutil.which(name)
        if browser:
            break

    if not browser:
        return 'no headless browser found; report.html and SVG chart are ready to open manually'

    report_path = (output_dir / 'report.html').resolve().as_uri()
    screenshot_path = output_dir / 'report_screenshot.png'
    result = subprocess.run(
        [
            browser,
            '--headless',
            '--no-sandbox',
            '--disable-gpu',
            '--hide-scrollbars',
            '--window-size=1440,2200',
            f'--screenshot={screenshot_path}',
            report_path,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors='replace',
        check=False,
    )
    if result.returncode == 0 and screenshot_path.exists():
        return str(screenshot_path)
    return 'screenshot command failed; see screenshot.log'


def main():
    timestamp = datetime.datetime.now().astimezone().strftime('%Y%m%d-%H%M%S')
    output_dir = EVIDENCE_ROOT / timestamp
    output_dir.mkdir(parents=True, exist_ok=False)

    environment = {
        'time': datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
        'platform': platform.platform(),
        'python': sys.version.split()[0],
        'repository': str(ROOT),
    }
    for tool in ('cargo', 'rustc', 'mn', 'ovs-vsctl', 'pox.py'):
        environment[tool] = shutil.which(tool) or 'not found on this PATH'

    checks = []
    outputs = {}
    for name, command, needs_root in CHECKS:
        check, output = run_check(name, command, needs_root, output_dir)
        checks.append(check)
        outputs[name] = output

    scheduler_rows, markers, evidence_lines = parse_results(outputs)
    with (output_dir / 'scheduler_results.csv').open('w', newline='', encoding='utf-8') as file:
        fields = [
            'name', 'algorithm', 'requests', 'h3', 'h4', 'h3_share',
            'h4_share', 'expectation', 'duration_seconds', 'requests_per_second',
        ]
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(scheduler_rows)

    results = {
        'environment': environment,
        'checks': checks,
        'scheduler_results': scheduler_rows,
        'observed_markers': markers,
        'summary_lines': evidence_lines,
    }
    (output_dir / 'results.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
    make_report(output_dir, checks, scheduler_rows, markers, evidence_lines, environment)
    screenshot_result = capture_screenshot(output_dir)
    (output_dir / 'screenshot_status.txt').write_text(screenshot_result + '\n', encoding='utf-8')

    print(f'\nEvidence saved to: {output_dir}')
    print(f'Report page: {output_dir / "report.html"}')
    print(f'Report screenshot: {screenshot_result}')
    if any(check['status'] != 'PASS' for check in checks):
        print('Some checks failed; use the report and per-check logs to see which ones.')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
