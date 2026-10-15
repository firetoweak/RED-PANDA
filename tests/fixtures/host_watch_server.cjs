const fs = require('fs');
const http = require('http');
let value = fs.readFileSync('value.txt', 'utf8');
fs.watch('.', () => {
  value = fs.readFileSync('value.txt', 'utf8');
  console.log('RELOAD', value);
});
const server = http.createServer((req, res) => {
  res.setHeader('content-type', 'application/json');
  res.end(JSON.stringify({pid: process.pid, value}));
});
server.listen(0, '127.0.0.1', () => {
  console.log(JSON.stringify({pid: process.pid, port: server.address().port}));
});
