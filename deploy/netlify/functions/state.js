const https = require('https');
const http = require('http');

exports.handler = async (event) => {
  return new Promise((resolve) => {
    const req = http.get('http://79.127.184.155:8197/api/state', (res) => {
      let data = '';
      res.on('data', chunk => data += chunk);
      res.on('end', () => {
        resolve({
          statusCode: 200,
          headers: {
            'Access-Control-Allow-Origin': '*',
            'Content-Type': 'application/json'
          },
          body: data
        });
      });
    }).on('error', (err) => {
      resolve({
        statusCode: 502,
        headers: {
          'Access-Control-Allow-Origin': '*',
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({ error: err.message, hint: 'Port 8197 must be publicly accessible or use server-side proxy' })
      });
    });
    req.setTimeout(5000, () => {
      req.destroy();
      resolve({
        statusCode: 504,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ error: 'Gateway timeout — server at 79.127.184.155:8197 unreachable' })
      });
    });
  });
};