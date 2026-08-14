/** @type {import('tailwindcss').Config} */
export default {
  darkMode: "class",
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Primary brand blue. Named separately from Tailwind's own `blue` so
        // every usage in the app is an intentional brand reference, not an
        // accidental match with a stock color that could later drift.
        brand: {
          50: "#eff6ff",
          100: "#dbeafe",
          200: "#bfdbfe",
          300: "#93c5fd",
          400: "#60a5fa",
          500: "#3b82f6",
          600: "#2563eb",
          700: "#1d4ed8",
          800: "#1e40af",
          900: "#1e3a8a",
          950: "#172554",
        },
      },
      boxShadow: {
        // Layered: a tight contact shadow plus a soft, wide ambient one --
        // reads as a raised, lit panel rather than a flat box with a line
        // under it, which is what a single-layer shadow always looks like.
        card: "0 1px 1px 0 rgb(15 23 42 / 0.03), 0 2px 6px -1px rgb(15 23 42 / 0.05), 0 8px 24px -8px rgb(15 23 42 / 0.06)",
        "card-dark":
          "0 1px 1px 0 rgb(0 0 0 / 0.3), 0 4px 12px -2px rgb(0 0 0 / 0.35), 0 16px 40px -12px rgb(0 0 0 / 0.5)",
      },
      backgroundImage: {
        "grid-pattern":
          "linear-gradient(to right, currentColor 1px, transparent 1px), linear-gradient(to bottom, currentColor 1px, transparent 1px)",
      },
      keyframes: {
        "drop-in": {
          "0%": { opacity: "0", transform: "translateY(-18px) scale(0.98)" },
          "60%": { opacity: "1", transform: "translateY(4px) scale(1.002)" },
          "100%": { opacity: "1", transform: "translateY(0) scale(1)" },
        },
        "fade-up": {
          "0%": { opacity: "0", transform: "translateY(10px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        "float-slow": {
          "0%, 100%": { transform: "translateY(0)" },
          "50%": { transform: "translateY(-10px)" },
        },
      },
      animation: {
        // Spring-like ease (overshoot then settle) for the login card and the
        // post-login shell dropping into place.
        "drop-in": "drop-in 620ms cubic-bezier(0.16, 1, 0.3, 1) both",
        "fade-up": "fade-up 420ms cubic-bezier(0.16, 1, 0.3, 1) both",
        "float-slow": "float-slow 6s ease-in-out infinite",
      },
    },
  },
  plugins: [],
};
