"use client";

import { MotionConfig } from "motion/react";
import { BrowserOverlayHost } from "./LiveBrowser";

export function Providers({ children }: { children: React.ReactNode }) {
  return (
    <MotionConfig reducedMotion="user">
      {children}
      <BrowserOverlayHost />
    </MotionConfig>
  );
}
