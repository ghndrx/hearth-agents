module.exports = {
  apps: [
    {
      name: 'hearth-agents',
      script: 'dist/main.js',
      interpreter: 'node',
      instances: 1,
      exec_mode: 'fork',
      autorestart: true,
      max_restarts: 10,
      min_uptime: '15m',
      watch: false,
      max_memory_restart: '512M',

      // Log configuration
      error_file: './logs/hearth-agents-error.log',
      out_file: './logs/hearth-agents-out.log',
      log_date_format: 'YYYY-MM-DD HH:mm:ss.SSS',
      merge_logs: true,

      // Log rotation
      log_type: 'json',
      max_size: '10M',
      retain: 5,

      // Environment: production (default)
      env: {
        NODE_ENV: 'production',
      },

      // Environment: development (--env dev)
      env_dev: {
        NODE_ENV: 'development',
        script: 'npx tsx src/main.ts',
      },
    },
  ],
};
