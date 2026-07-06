import { useCallback, useRef } from 'react';

// Returns a stable-identity function that always invokes the latest `fn`.
// Lets memoized child components receive handlers without re-rendering when
// the parent re-creates the handler each render. Event/callback use only —
// never call the result during render.
export default function useEventCallback(fn) {
  const ref = useRef(fn);
  ref.current = fn;
  return useCallback((...args) => ref.current(...args), []);
}
