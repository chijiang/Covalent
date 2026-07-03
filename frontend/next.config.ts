import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "standalone", // Enable standalone output for Docker deployment
  // Allow cross-origin requests to images from other domains
  allowedDevOrigins: process.env.ALLOWED_DEV_ORIGINS 
    ? process.env.ALLOWED_DEV_ORIGINS.split(',').map(s => s.trim()) 
    : [],
};

export default nextConfig;
