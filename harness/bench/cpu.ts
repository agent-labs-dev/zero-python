// The CPU-only cases (harness/bench/cpu_cases.ts) as CodSpeed benchmarks, named as bench/test_cpu.py
// names the Python side, so CodSpeed shows the two next to each other.
//
//   node harness/bench/cpu.ts
//
// Under the CodSpeed action the plugin measures each case by instruction count; elsewhere tinybench
// times it briefly, which only checks that each case runs.
import {withCodSpeed} from '@codspeed/tinybench-plugin';
import {Bench} from 'tinybench';
import {cpuCases} from './cpu_cases.ts';
import type {Any} from './workloads.ts';

// Upstream logs the failures it handles.
console.log = console.info = console.debug = console.warn = console.error = () => {};
const print = (s: string) => process.stdout.write(s + '\n');

const bench = withCodSpeed(new Bench({time: 100}));
for (const [name, fn] of await cpuCases()) bench.add(name, fn);
await bench.run();
for (const task of bench.tasks) {
  const r = task.result as Any;
  print(`${task.name.padEnd(28)} ${r?.latency?.mean ? `${(r.latency.mean * 1000).toFixed(1)} µs` : JSON.stringify(r?.error ?? r?.state ?? '')}`);
}
