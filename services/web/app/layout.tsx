import type { Metadata, Viewport } from "next";
import "@fontsource-variable/inter";
import "@fontsource-variable/jetbrains-mono";
import "./globals.css";
import { Nav, TabBar } from "@/components/Nav";
import { Providers } from "@/components/Providers";

export const metadata: Metadata = {
  title: "Todd",
  description: "Self-hosted multi-agent operator",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#eceff6" },
    { media: "(prefers-color-scheme: dark)", color: "#06070a" },
  ],
};

// Applies the saved theme before first paint (no flash).
const themeScript = `try{var t=localStorage.getItem("todd.theme");if(t==="light"||t==="dark")document.documentElement.dataset.theme=t}catch(e){}`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeScript }} />
      </head>
      <body className="min-h-dvh">
        <div className="wallpaper" aria-hidden />
        <Providers>
          <div className="flex min-h-dvh">
            <Nav />
            <main className="min-w-0 flex-1 pb-28 md:pb-0">{children}</main>
          </div>
          <TabBar />
        </Providers>
      </body>
    </html>
  );
}
