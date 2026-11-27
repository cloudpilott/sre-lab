// k6 load test: ramps to 50 virtual users. Thresholds mirror the SLOs.
import http from 'k6/http';
import { check, sleep } from 'k6';

const BASE = __ENV.BASE_URL || 'http://localhost:9898';

export const options = {
  scenarios: {
    ramp: {
      executor: 'ramping-vus',
      stages: [
        { duration: '1m', target: 20 },
        { duration: '3m', target: 50 },
        { duration: '1m', target: 0 },
      ],
    },
  },
  thresholds: {
    http_req_failed: ['rate<0.005'],     // availability SLO 99.5%
    http_req_duration: ['p(99)<250'],    // latency SLO 99% < 250ms
  },
};

export default function () {
  const paths = ['/', '/api/info', '/healthz', '/env'];
  const r = http.get(`${BASE}${paths[Math.floor(Math.random() * paths.length)]}`);
  check(r, { 'status is 2xx': (res) => res.status >= 200 && res.status < 300 });
  sleep(0.2);
}
