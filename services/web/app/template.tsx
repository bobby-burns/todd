"use client";

import { motion } from "motion/react";
import { pageSpring } from "@/lib/motion";

/** Route transition. Only transform + opacity: a filter here would cut the glass off from the wallpaper. */
export default function Template({ children }: { children: React.ReactNode }) {
  return (
    <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={pageSpring}>
      {children}
    </motion.div>
  );
}
