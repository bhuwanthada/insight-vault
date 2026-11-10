import "./globals.css";

export const metadata = {
  title: "Insight Vault",
  description: "Highlight and tag interview quotes, then build insights that stay linked to their evidence.",
};

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
