export default {
  async rewrites() {
    return [{source: '/api/:path*', destination: 'http://business:8080/api/:path*'}];
  }
};
