// The native Android client needs no offline content cache.
// Deliberately never store private articles, images, session responses or comments.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));
