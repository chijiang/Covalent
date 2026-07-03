"use client";

import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type MouseEvent as ReactMouseEvent,
} from "react";

type UseResizablePanelOptions = {
  collapseMediaQuery: string;
  defaultWidth: number;
  maxPanelWidth: number;
  minPanelWidth: number;
  minRemainingWidth: number;
  storageKey: string;
  step?: number;
  widthCssVar?: `--${string}`;
};

function getMaxPanelWidth(
  containerWidth: number,
  maxPanelWidth: number,
  minPanelWidth: number,
  minRemainingWidth: number,
): number {
  if (!Number.isFinite(containerWidth) || containerWidth <= 0) {
    return maxPanelWidth;
  }

  return Math.max(minPanelWidth, Math.min(maxPanelWidth, containerWidth - minRemainingWidth));
}

function clampPanelWidth(
  value: number,
  containerWidth: number,
  maxPanelWidth: number,
  minPanelWidth: number,
  minRemainingWidth: number,
): number {
  const nextMaxWidth = getMaxPanelWidth(containerWidth, maxPanelWidth, minPanelWidth, minRemainingWidth);
  return Math.min(nextMaxWidth, Math.max(minPanelWidth, Math.round(value)));
}

export function useResizablePanel({
  collapseMediaQuery,
  defaultWidth,
  maxPanelWidth,
  minPanelWidth,
  minRemainingWidth,
  storageKey,
  step = 24,
  widthCssVar = "--console-list-width",
}: UseResizablePanelOptions) {
  const splitRef = useRef<HTMLDivElement | null>(null);
  const [containerWidth, setContainerWidth] = useState(0);
  const [panelWidth, setPanelWidth] = useState(defaultWidth);
  const [isResizing, setIsResizing] = useState(false);

  useEffect(() => {
    if (typeof window === "undefined") {
      return;
    }

    const stored = window.localStorage.getItem(storageKey);
    if (!stored) {
      return;
    }

    const parsed = Number(stored);
    if (Number.isFinite(parsed)) {
      const frame = window.requestAnimationFrame(() => {
        setPanelWidth(parsed);
      });
      return () => {
        window.cancelAnimationFrame(frame);
      };
    }
  }, [storageKey]);

  useEffect(() => {
    if (typeof window === "undefined") {
      return;
    }

    window.localStorage.setItem(storageKey, `${panelWidth}`);
  }, [panelWidth, storageKey]);

  useEffect(() => {
    const splitLayout = splitRef.current;
    if (!splitLayout || typeof ResizeObserver === "undefined") {
      return;
    }

    const syncWidth = (containerWidth: number) => {
      setContainerWidth(containerWidth);
      setPanelWidth((current) =>
        clampPanelWidth(current, containerWidth, maxPanelWidth, minPanelWidth, minRemainingWidth),
      );
    };

    syncWidth(splitLayout.clientWidth);

    const observer = new ResizeObserver((entries) => {
      const nextWidth = entries[0]?.contentRect.width ?? splitLayout.clientWidth;
      syncWidth(nextWidth);
    });

    observer.observe(splitLayout);
    return () => {
      observer.disconnect();
    };
  }, [maxPanelWidth, minPanelWidth, minRemainingWidth]);

  const panelStyle = useMemo(
    () =>
      ({
        [widthCssVar]: `${panelWidth}px`,
      }) as CSSProperties,
    [panelWidth, widthCssVar],
  );

  const panelWidthMax = getMaxPanelWidth(containerWidth, maxPanelWidth, minPanelWidth, minRemainingWidth);

  const isCollapsedViewport = () =>
    typeof window !== "undefined" && window.matchMedia(collapseMediaQuery).matches;

  function handleResizeStart(event: ReactMouseEvent<HTMLDivElement>) {
    if (event.button !== 0 || isCollapsedViewport()) {
      return;
    }

    const splitLayout = splitRef.current;
    if (!splitLayout) {
      return;
    }

    event.preventDefault();
    event.currentTarget.focus();

    const startX = event.clientX;
    const startWidth = panelWidth;
    const rootStyle = document.documentElement.style;
    const previousCursor = rootStyle.cursor;
    const previousUserSelect = rootStyle.userSelect;

    setIsResizing(true);
    rootStyle.cursor = "col-resize";
    rootStyle.userSelect = "none";

    const handlePointerMove = (moveEvent: globalThis.MouseEvent) => {
      const delta = moveEvent.clientX - startX;
      setPanelWidth(
        clampPanelWidth(
          startWidth + delta,
          splitLayout.clientWidth,
          maxPanelWidth,
          minPanelWidth,
          minRemainingWidth,
        ),
      );
    };

    const stopResizing = () => {
      setIsResizing(false);
      rootStyle.cursor = previousCursor;
      rootStyle.userSelect = previousUserSelect;
      window.removeEventListener("mousemove", handlePointerMove);
      window.removeEventListener("mouseup", stopResizing);
    };

    window.addEventListener("mousemove", handlePointerMove);
    window.addEventListener("mouseup", stopResizing);
  }

  function handleResizeKeyDown(event: ReactKeyboardEvent<HTMLDivElement>) {
    if (isCollapsedViewport()) {
      return;
    }

    const splitLayout = splitRef.current;
    if (!splitLayout) {
      return;
    }

    const nextMaxWidth = getMaxPanelWidth(
      splitLayout.clientWidth,
      maxPanelWidth,
      minPanelWidth,
      minRemainingWidth,
    );
    let nextWidth: number | null = null;

    if (event.key === "ArrowLeft") {
      nextWidth = panelWidth - step;
    } else if (event.key === "ArrowRight") {
      nextWidth = panelWidth + step;
    } else if (event.key === "Home") {
      nextWidth = minPanelWidth;
    } else if (event.key === "End") {
      nextWidth = nextMaxWidth;
    }

    if (nextWidth === null) {
      return;
    }

    event.preventDefault();
    setPanelWidth(
      clampPanelWidth(nextWidth, splitLayout.clientWidth, maxPanelWidth, minPanelWidth, minRemainingWidth),
    );
  }

  return {
    handleResizeKeyDown,
    handleResizeStart,
    isResizing,
    panelStyle,
    panelWidth,
    panelWidthMax,
    panelWidthMin: minPanelWidth,
    splitRef,
  };
}
