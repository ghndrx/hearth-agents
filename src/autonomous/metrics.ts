import { createServer, type IncomingMessage, type ServerResponse } from 'node:http';

// ---------------------------------------------------------------------------
// Counter
// ---------------------------------------------------------------------------

interface CounterLabels {
  [key: string]: string;
}

class Counter {
  private readonly name: string;
  private readonly help: string;
  private readonly labelNames: string[];
  private readonly values = new Map<string, number>();

  constructor(name: string, help: string, labelNames: string[] = []) {
    this.name = name;
    this.help = help;
    this.labelNames = labelNames;
  }

  inc(labels: CounterLabels = {}, value = 1): void {
    const key = this.labelKey(labels);
    this.values.set(key, (this.values.get(key) ?? 0) + value);
  }

  toPrometheus(): string {
    const lines: string[] = [
      `# HELP ${this.name} ${this.help}`,
      `# TYPE ${this.name} counter`,
    ];
    for (const [key, value] of this.values) {
      const labelStr = key ? `{${key}}` : '';
      lines.push(`${this.name}${labelStr} ${value}`);
    }
    return lines.join('\n');
  }

  private labelKey(labels: CounterLabels): string {
    return this.labelNames
      .filter((n) => labels[n] !== undefined)
      .map((n) => `${n}="${labels[n]}"`)
      .join(',');
  }
}

// ---------------------------------------------------------------------------
// Gauge
// ---------------------------------------------------------------------------

class Gauge {
  private readonly name: string;
  private readonly help: string;
  private value = 0;

  constructor(name: string, help: string) {
    this.name = name;
    this.help = help;
  }

  set(v: number): void {
    this.value = v;
  }

  inc(v = 1): void {
    this.value += v;
  }

  dec(v = 1): void {
    this.value -= v;
  }

  toPrometheus(): string {
    return [
      `# HELP ${this.name} ${this.help}`,
      `# TYPE ${this.name} gauge`,
      `${this.name} ${this.value}`,
    ].join('\n');
  }
}

// ---------------------------------------------------------------------------
// Histogram
// ---------------------------------------------------------------------------

class Histogram {
  private readonly name: string;
  private readonly help: string;
  private readonly labelNames: string[];
  private readonly bucketBounds: number[];
  private readonly data = new Map<
    string,
    { buckets: number[]; sum: number; count: number }
  >();

  constructor(
    name: string,
    help: string,
    labelNames: string[] = [],
    bucketBounds: number[] = [0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10],
  ) {
    this.name = name;
    this.help = help;
    this.labelNames = labelNames;
    this.bucketBounds = bucketBounds.sort((a, b) => a - b);
  }

  observe(labels: CounterLabels, value: number): void {
    const key = this.labelKey(labels);
    let entry = this.data.get(key);
    if (!entry) {
      entry = { buckets: new Array(this.bucketBounds.length).fill(0), sum: 0, count: 0 };
      this.data.set(key, entry);
    }
    entry.sum += value;
    entry.count += 1;
    for (let i = 0; i < this.bucketBounds.length; i++) {
      if (value <= this.bucketBounds[i]) {
        entry.buckets[i] += 1;
      }
    }
  }

  toPrometheus(): string {
    const lines: string[] = [
      `# HELP ${this.name} ${this.help}`,
      `# TYPE ${this.name} histogram`,
    ];
    for (const [key, entry] of this.data) {
      const baseLabel = key ? `${key},` : '';
      for (let i = 0; i < this.bucketBounds.length; i++) {
        lines.push(
          `${this.name}_bucket{${baseLabel}le="${this.bucketBounds[i]}"} ${entry.buckets[i]}`,
        );
      }
      lines.push(`${this.name}_bucket{${baseLabel}le="+Inf"} ${entry.count}`);
      lines.push(`${this.name}_sum{${key ? key : ''}} ${entry.sum}`);
      lines.push(`${this.name}_count{${key ? key : ''}} ${entry.count}`);
    }
    return lines.join('\n');
  }

  private labelKey(labels: CounterLabels): string {
    return this.labelNames
      .filter((n) => labels[n] !== undefined)
      .map((n) => `${n}="${labels[n]}"`)
      .join(',');
  }
}

// ---------------------------------------------------------------------------
// Metric instances
// ---------------------------------------------------------------------------

const apiCallsTotal = new Counter(
  'api_calls_total',
  'Total number of API calls',
  ['provider', 'model', 'status'],
);

const featuresCompletedTotal = new Counter(
  'features_completed_total',
  'Total number of features completed',
);

const tokensUsedTotal = new Counter(
  'tokens_used_total',
  'Total tokens used across providers',
  ['provider', 'direction'],
);

const activeFeatures = new Gauge(
  'active_features',
  'Number of features currently in progress',
);

const backlogSize = new Gauge(
  'backlog_size',
  'Number of features in the backlog',
);

const apiCallDuration = new Histogram(
  'api_call_duration_seconds',
  'Duration of API calls in seconds',
  ['provider'],
);

// ---------------------------------------------------------------------------
// Public helpers
// ---------------------------------------------------------------------------

export function recordApiCall(
  provider: string,
  model: string,
  status: string,
  durationMs: number,
  inputTokens: number,
  outputTokens: number,
): void {
  apiCallsTotal.inc({ provider, model, status });
  tokensUsedTotal.inc({ provider, direction: 'input' }, inputTokens);
  tokensUsedTotal.inc({ provider, direction: 'output' }, outputTokens);
  apiCallDuration.observe({ provider }, durationMs / 1000);
}

export function recordFeatureComplete(): void {
  featuresCompletedTotal.inc();
}

export { activeFeatures, backlogSize };

// ---------------------------------------------------------------------------
// Prometheus text export
// ---------------------------------------------------------------------------

export function getMetricsText(): string {
  return [
    apiCallsTotal.toPrometheus(),
    featuresCompletedTotal.toPrometheus(),
    tokensUsedTotal.toPrometheus(),
    activeFeatures.toPrometheus(),
    backlogSize.toPrometheus(),
    apiCallDuration.toPrometheus(),
  ].join('\n\n') + '\n';
}

// ---------------------------------------------------------------------------
// HTTP metrics server
// ---------------------------------------------------------------------------

let server: ReturnType<typeof createServer> | null = null;

export function startMetricsServer(port = 9090): void {
  if (server) return;

  server = createServer((req: IncomingMessage, res: ServerResponse) => {
    if (req.url === '/metrics' && req.method === 'GET') {
      res.writeHead(200, { 'Content-Type': 'text/plain; version=0.0.4; charset=utf-8' });
      res.end(getMetricsText());
    } else {
      res.writeHead(404, { 'Content-Type': 'text/plain' });
      res.end('Not Found');
    }
  });

  server.listen(port, () => {
    console.log(`[metrics] Prometheus metrics server listening on :${port}/metrics`);
  });
}
