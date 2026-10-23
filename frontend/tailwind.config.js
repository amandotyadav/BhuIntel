/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: { sans: ["Inter", "system-ui", "Segoe UI", "Roboto", "sans-serif"] },
      colors: {
        brand: { 50: "#eef6f4", 100: "#d5ebe5", 500: "#1f7a64", 600: "#17654f", 700: "#11503f", 900: "#0a2f25" },
      },
    },
  },
  plugins: [],
};
