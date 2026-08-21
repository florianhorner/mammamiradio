// The page's phase decisions as a pure function, because node:test has no
// audio element. app.js feeds it what the <audio> reported; it answers what
// the page should show. All the branching that used to live inside event
// handlers is testable here without a browser.
//
// Inputs:
//   phase        "idle" | "onair" | "revealed"
//   positionSec  current playback position (0 when nothing played yet)
//   revealAtSec  the cue point where the home moment lands, or null while
//                clips predate the produced manifest (then: reveal on end)
//   ended        the clip finished
//   failed       a REPORTED failure: 'error' event or a rejected play()
//   deadline     the silent case: nothing advanced before the deadline —
//                no event ever fired, so only a timer can catch it
//   buffering    'waiting'/'stalled' without error — loading, not failure
//
// Output: { phase, audio } where audio is "" | "loading" | "failed".
// The invariant behind the shape (aired-truth, applied to the page): a
// visitor who heard nothing is never shown a successful on-air moment, and
// a visitor is never left on a frozen page. Failure therefore still reveals
// — but with audio:"failed", which renders the transcript as text, not as
// a moment that aired.

export function nextPhase({ phase, positionSec = 0, revealAtSec = null, ended = false, failed = false, deadline = false, buffering = false }) {
  if (failed || deadline) return { phase: "revealed", audio: "failed" };
  if (phase === "idle") return { phase: "idle", audio: "" };
  if (ended) return { phase: "revealed", audio: "" };
  if (revealAtSec !== null && positionSec >= revealAtSec) {
    return { phase: "revealed", audio: buffering ? "loading" : "" };
  }
  if (phase === "revealed") return { phase: "revealed", audio: buffering ? "loading" : "" };
  return { phase: "onair", audio: buffering ? "loading" : "" };
}

export default nextPhase;
