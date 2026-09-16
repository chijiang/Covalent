"use client";

import { useEffect, useMemo, useRef } from "react";
import * as echarts from "echarts/core";
import { BarChart, LineChart, PieChart, ScatterChart } from "echarts/charts";
import {
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  TitleComponent,
  TooltipComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";

echarts.use([
  BarChart,
  LineChart,
  PieChart,
  ScatterChart,
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  TitleComponent,
  TooltipComponent,
  CanvasRenderer,
]);

const containerStyle = {
  width: "100%",
  height: 320,
  borderRadius: 12,
  background: "#ffffff",
  boxShadow: "inset 0 0 0 1px rgba(15, 23, 42, 0.08)",
  overflow: "hidden",
} as const;

const placeholderStyle = {
  ...containerStyle,
  height: 120,
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  color: "#64748b",
  fontSize: 12,
} as const;

/**
 * Renders an agent-authored ```echarts block: the body is one ECharts option
 * JSON object. While the spec is still streaming in, JSON.parse fails and a
 * placeholder is shown; once complete it renders as an interactive chart.
 */
export function EChartsBlock({ spec }: { spec: string }) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const option = useMemo<Record<string, object> | null>(() => {
    try {
      const parsed: unknown = JSON.parse(spec);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        return parsed as Record<string, object>;
      }
      return null;
    } catch {
      return null;
    }
  }, [spec]);

  useEffect(() => {
    const container = containerRef.current;
    if (!option || !container) {
      return;
    }
    let chart: echarts.ECharts | null = null;
    try {
      chart = echarts.init(container);
      chart.setOption(option as Parameters<echarts.ECharts["setOption"]>[0], { notMerge: true });
    } catch {
      chart?.dispose();
      return;
    }
    const observer = new ResizeObserver(() => chart?.resize());
    observer.observe(container);
    return () => {
      observer.disconnect();
      chart?.dispose();
      chart = null;
    };
  }, [option]);

  if (!option) {
    return (
      <div style={placeholderStyle} role="status">
        Chart is being generated…
      </div>
    );
  }
  return <div ref={containerRef} style={containerStyle} />;
}
