import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "闲鱼雷达",
  description: "闲鱼商品监控与邮件推送控制台",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
