// The standalone service only binds 127.0.0.1 (it refuses other hosts by design). Inside its own
// container a plain TCP relay on 18901 lets the ark-watch daemon reach it over the isolated ofm-net.
import net from 'node:net'

net
  .createServer((client) => {
    const upstream = net.connect(18900, '127.0.0.1')
    client.pipe(upstream).pipe(client)
    client.on('error', () => upstream.destroy())
    upstream.on('error', () => client.destroy())
  })
  .listen(18901, '0.0.0.0')
