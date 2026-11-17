// The container's nginx proxies /api to the api service, so the web build stays origin-relative.
// A native (Capacitor) build would call the API from another origin. The API sends no CORS
// headers today, so that needs CORS support added to the web app first (src/app/web/main.py).
export const environment = {
  production: true,
  apiUrl: '/api',
};
