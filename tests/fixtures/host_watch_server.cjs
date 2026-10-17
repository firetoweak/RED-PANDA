const fs = require('fs');
const http = require('http');
let value = fs.readFileSync('value.txt', 'utf8');
function reload() {
  try {
    value = fs.readFileSync('value.txt', 'utf8');
  } catch (error) {
    // Windows reports the change while the writer still holds the file
    // exclusively; read the published content once it is released.
    if (error.code !== 'EBUSY') throw error;
    setTimeout(reload, 10);
    return;
  }
  console.log('RELOAD', value);
}
fs.watch('.', reload);
const server = http.createServer((req, res) => {
  res.setHeader('content-type', 'application/json');
  res.end(JSON.stringify({pid: process.pid, value}));
});
server.listen(0, '127.0.0.1', () => {
  console.log(JSON.stringify({pid: process.pid, port: server.address().port}));
});
