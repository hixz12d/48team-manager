chrome.webRequest.onBeforeRequest.addListener(
  function () { return { cancel: true }; },
  {
    urls: [
      "*://www.google-analytics.com/*",
      "*://*.google-analytics.com/*",
      "*://*.googletagmanager.com/*",
      "*://*.doubleclick.net/*",
      "*://*.facebook.com/*",
      "*://*.facebook.net/*",
      "*://*.sentry.io/*",
      "*://browser.sentry-cdn.com/*",
      "*://*.intercom.io/*",
      "*://*.intercomcdn.com/*",
      "*://*.hotjar.com/*",
      "*://*.segment.io/*",
      "*://*.segment.com/*",
      "*://*.amplitude.com/*",
      "*://*.mixpanel.com/*",
      "*://*.datadoghq.com/*",
      "*://*.clarity.ms/*",
      "*://*.newrelic.com/*",
      "*://*.nr-data.net/*"
    ]
  },
  ["blocking"]
);
