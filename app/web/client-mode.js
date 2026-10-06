// Choose once on page load. Keyboard opening/rotation must never reconnect a
// terminal. An explicit selector change is the only in-page client switch.
(function (root) {
  function resolveMode(preference, coarsePointer, width) {
    if (preference === 'desktop' || preference === 'mobile') return preference;
    return coarsePointer && width <= 1024 ? 'mobile' : 'desktop';
  }
  root.devboxClientMode = {resolveMode};
  if (typeof module !== 'undefined') module.exports = root.devboxClientMode;
})(typeof window === 'undefined' ? globalThis : window);
