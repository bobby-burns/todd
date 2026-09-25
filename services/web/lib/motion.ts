import type { Transition, Variants } from "motion/react";

/** Snappy spring for controls (segmented thumbs, switches, pills). */
export const spring: Transition = { type: "spring", stiffness: 520, damping: 38, mass: 0.7 };
/** Softer spring for panels, sheets and layout changes. */
export const softSpring: Transition = { type: "spring", stiffness: 280, damping: 32, mass: 0.9 };
/** Very soft spring for page-level content. */
export const pageSpring: Transition = { type: "spring", stiffness: 200, damping: 28, mass: 1 };

export const fadeUp: Variants = {
  hidden: { opacity: 0, y: 10 },
  show: { opacity: 1, y: 0, transition: softSpring },
};

export const stagger = (step = 0.035, delay = 0): Variants => ({
  hidden: {},
  show: { transition: { staggerChildren: step, delayChildren: delay } },
});

/** Events stream in: a short rise with a touch of blur (rows contain no glass, so filter is safe here). */
export const eventIn = {
  initial: { opacity: 0, y: 6, filter: "blur(3px)" },
  animate: { opacity: 1, y: 0, filter: "blur(0px)" },
  transition: { type: "spring", stiffness: 360, damping: 32, mass: 0.7 } as Transition,
};
