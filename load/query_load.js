// k6 load test: sustain ~1k QPS of single-source queries for 60s.
//
// Run the server with raised rate-limit budgets so the limiter doesn't throttle
// the load generator, then point k6 at it:
//
//   USQL_RATELIMIT_RPM_MULTIPLIER=100000 uvicorn src.main:app --port 8099
//   TOKEN=$(python -m src.auth eng acme-corp) \
//     k6 run -e TOKEN=$TOKEN load/query_load.js
//
// The cache makes repeated identical queries cheap (L1 hit), so this measures
// gateway + planner + entitlement + cache overhead under concurrency.

import http from "k6/http";
import { check } from "k6";

const BASE = __ENV.BASE_URL || "http://localhost:8099";
const TOKEN = __ENV.TOKEN;
const RATE = parseInt(__ENV.RATE || "1000", 10);

export const options = {
  scenarios: {
    steady: {
      executor: "constant-arrival-rate",
      rate: RATE,
      timeUnit: "1s",
      duration: "60s",
      preAllocatedVUs: 200,
      maxVUs: 800,
    },
  },
  thresholds: {
    // SLO from the design doc for single-source predicate-pushdown queries.
    http_req_duration: ["p(95)<1500"],
    http_req_failed: ["rate<0.01"],
  },
};

const PAYLOAD = JSON.stringify({
  sql: "SELECT pr.id, pr.state FROM github.pull_requests pr WHERE pr.state = 'open' LIMIT 10",
});

const PARAMS = {
  headers: {
    "Content-Type": "application/json",
    Authorization: `Bearer ${TOKEN}`,
  },
};

export default function () {
  const res = http.post(`${BASE}/v1/query`, PAYLOAD, PARAMS);
  check(res, {
    "status is 200": (r) => r.status === 200,
    "has rows": (r) => r.json("rows") !== undefined,
  });
}
