type FocusTimeline = (outputId: string) => void;

let focusTimelineImpl: FocusTimeline = () => {};

export function registerTimelineFocus(focus: FocusTimeline): () => void {
  focusTimelineImpl = focus;
  return () => {
    if (focusTimelineImpl === focus) {
      focusTimelineImpl = () => {};
    }
  };
}

export function focusTimeline(outputId: string): void {
  focusTimelineImpl(outputId);
}
