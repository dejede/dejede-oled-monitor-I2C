'use strict';
'require view';
'require form';
'require fs';
'require ui';
'require dom';
'require poll';
'require uci';

var SVC = '/etc/init.d/oled_monitor',
    LOG = '/var/log/oled_monitor.log',
    SECTIONS = { global: 'global', main: 'scene', weather: 'scene', ticker: 'scene' };

return view.extend({
	load: function() {
		/* pastikan semua section ada (konfigurasi lama / baru dipasang) */
		return uci.load('oled_monitor').then(function() {
			Object.keys(SECTIONS).forEach(function(name) {
				if (uci.get('oled_monitor', name) == null)
					uci.add('oled_monitor', SECTIONS[name], name);
			});
		});
	},

	renderStatus: function() {
		var stEl = E('strong', {}, '…'),
		    logEl = E('pre', { 'style': 'max-height:11em;overflow:auto;font-size:80%;margin:.5em 0 0' }, '');

		var update = function() {
			return Promise.all([
				L.resolveDefault(fs.exec(SVC, [ 'status' ]), null),
				L.resolveDefault(fs.read_direct(LOG), '')
			]).then(function(r) {
				var ok = r[0] && String(r[0].stdout || '').trim() === 'running';
				stEl.textContent = ok ? _('Berjalan') : _('Berhenti');
				stEl.style.color = ok ? '#2a9d3a' : '#d33';
				logEl.textContent = String(r[1] || '').trim().split('\n').slice(-12).join('\n');
			});
		};

		var act = function(cmd) {
			return fs.exec(SVC, [ cmd ]).then(function() {
				return new Promise(function(res) { window.setTimeout(res, 1200); });
			}).then(update);
		};

		poll.add(update, 5);
		update();

		return E('div', { 'class': 'cbi-section' }, [
			E('h3', {}, _('Status layanan')),
			E('p', {}, [ _('Layanan: '), stEl, ' ',
				E('button', { 'class': 'cbi-button cbi-button-action',
					'click': ui.createHandlerFn(this, function() { return act('restart'); }) },
					_('Restart')), ' ',
				E('button', { 'class': 'cbi-button cbi-button-negative',
					'click': ui.createHandlerFn(this, function() { return act('stop'); }) },
					_('Stop')), ' ',
				E('button', { 'class': 'cbi-button cbi-button-positive',
					'click': ui.createHandlerFn(this, function() { return act('start'); }) },
					_('Start'))
			]),
			E('details', {}, [ E('summary', {}, _('Log terakhir')), logEl ])
		]);
	},

	render: function() {
		var m, s, o;

		m = new form.Map('oled_monitor', _('Dejede OLEDMON'),
			_('Atur tampilan OLED SSD1306 128x64. Setelah Save & Apply, perubahan dibaca otomatis oleh layanan dalam ±2 detik tanpa restart.'));

		/* ---------- Global ---------- */
		s = m.section(form.NamedSection, 'global', 'global', _('Tema & Transisi'));
		s.addremove = false;

		o = s.option(form.ListValue, 'theme', _('Tema warna'));
		o.value('normal', _('Normal (teks putih di latar hitam)'));
		o.value('inverted', _('Terbalik (teks hitam di latar putih)'));
		o.default = 'normal';

		o = s.option(form.Value, 'contrast', _('Kecerahan (kontras)'), _('0 – 255.'));
		o.datatype = 'range(0,255)';
		o.default = '255';
		o.placeholder = '255';

		o = s.option(form.Flag, 'flip', _('Putar layar 180°'), _('Untuk OLED yang terpasang terbalik.'));
		o.default = '0';

		o = s.option(form.ListValue, 'transition', _('Efek transisi antar scene'));
		o.value('none', _('Tanpa transisi'));
		o.value('random', _('Acak'));
		o.value('slide_left', _('Geser ke kiri'));
		o.value('slide_right', _('Geser ke kanan'));
		o.value('slide_up', _('Geser ke atas'));
		o.value('slide_down', _('Geser ke bawah'));
		o.value('wipe', _('Sapuan (wipe)'));
		o.value('open', _('Buka dari tengah'));
		o.value('blinds', _('Kerai (blinds)'));
		o.value('dissolve', _('Larut (dissolve)'));
		o.default = 'slide_left';

		o = s.option(form.Value, 'transition_ms', _('Durasi transisi'), _('Milidetik (0 – 2000). 0 = tanpa transisi.'));
		o.datatype = 'range(0,2000)';
		o.default = '600';
		o.placeholder = '600';

		/* ---------- Scene utama ---------- */
		s = m.section(form.NamedSection, 'main', 'scene', _('Scene Utama (sinyal & jaringan)'));
		s.addremove = false;

		o = s.option(form.Flag, 'enable', _('Aktifkan scene'));
		o.default = '1';

		o = s.option(form.Value, 'duration', _('Durasi tampil'), _('Detik. Default 60 (1 menit).'));
		o.datatype = 'range(3,3600)';
		o.default = '60';
		o.placeholder = '60';

		o = s.option(form.ListValue, 'layout', _('Tata letak'));
		o.value('classic', _('Klasik (angka UP / DWN)'));
		o.value('graph', _('Grafik (line chart upload & download)'));
		o.default = 'classic';

		o = s.option(form.Flag, 'marquee', _('Tampilkan running text'));
		o.default = '1';

		o = s.option(form.Value, 'text', _('Isi running text'), _('Maks. 160 karakter. Karakter di luar Latin-1 diganti “?”.'));
		o.maxlength = 160;
		o.default = '>> DEJEDE : 085236578999 - PHOTO & VIDEO SERVICES <<';
		o.depends('marquee', '1');

		o = s.option(form.Value, 'speed', _('Kecepatan running text'), _('Piksel per detik (10 – 150).'));
		o.datatype = 'range(10,150)';
		o.default = '40';
		o.placeholder = '40';
		o.depends('marquee', '1');

		/* ---------- Scene cuaca ---------- */
		s = m.section(form.NamedSection, 'weather', 'scene', _('Scene Cuaca'));
		s.addremove = false;

		o = s.option(form.Flag, 'enable', _('Aktifkan scene'));
		o.default = '1';

		o = s.option(form.Value, 'duration', _('Durasi tampil'), _('Detik. Default 20.'));
		o.datatype = 'range(3,3600)';
		o.default = '20';
		o.placeholder = '20';

		o = s.option(form.ListValue, 'layout', _('Tata letak'));
		o.value('classic', _('Klasik (suhu besar)'));
		o.value('detail', _('Rinci (suhu, lembap, angin, terasa)'));
		o.default = 'classic';

		o = s.option(form.Value, 'city', _('Nama kota'), _('Dipakai untuk judul dan pencarian koordinat otomatis bila lat/lon kosong.'));
		o.maxlength = 20;
		o.placeholder = 'Jakarta';

		o = s.option(form.Value, 'lat', _('Lintang (lat)'), _('Opsional. Jika lat & lon diisi, nama kota hanya jadi judul.'));
		o.datatype = 'float';
		o.optional = true;
		o.placeholder = '-6.2088';

		o = s.option(form.Value, 'lon', _('Bujur (lon)'));
		o.datatype = 'float';
		o.optional = true;
		o.placeholder = '106.8456';

		o = s.option(form.DummyValue, '_geo', _('Cari lokasi'),
			_('Cari nama kota lalu klik hasilnya untuk mengisi kota, lat, dan lon otomatis.'));
		o.renderWidget = function(section_id) {
			var self = this,
			    inp = E('input', { 'type': 'text', 'class': 'cbi-input-text', 'style': 'width:14em',
				    'placeholder': _('mis. Bandung') }),
			    out = E('div', { 'style': 'margin-top:.5em' });

			var pick = function(r) {
				var set = function(name, val) {
					var l = self.map.lookupOption(name, section_id);
					if (l && l[0]) l[0].getUIElement(l[1]).setValue(String(val));
				};
				set('city', r.name);
				set('lat', r.latitude);
				set('lon', r.longitude);
				dom.content(out, E('em', {}, _('Terisi: ') + r.name + ' (' + r.latitude + ', ' + r.longitude + ') — ' +
					_('klik Save & Apply untuk menerapkan.')));
			};

			var search = function() {
				var q = inp.value.trim();
				if (!q) return;
				dom.content(out, _('Mencari…'));
				return fetch('https://geocoding-api.open-meteo.com/v1/search?count=6&language=id&format=json&name=' +
					encodeURIComponent(q))
					.then(function(r) { return r.json(); })
					.then(function(j) {
						var res = j.results || [];
						if (!res.length) { dom.content(out, _('Tidak ditemukan.')); return; }
						dom.content(out, res.map(function(r) {
							var label = [ r.name, r.admin1, r.country ].filter(Boolean).join(', ') +
								'  (' + r.latitude.toFixed(4) + ', ' + r.longitude.toFixed(4) + ')';
							return E('div', {}, E('a', { 'href': '#', 'click': function(ev) {
								ev.preventDefault(); pick(r);
							} }, label));
						}));
					})
					.catch(function(e) { dom.content(out, _('Gagal mencari (butuh internet di browser): ') + e.message); });
			};

			inp.addEventListener('keydown', function(ev) {
				if (ev.key === 'Enter') { ev.preventDefault(); search(); }
			});
			return E('div', {}, [ inp, ' ',
				E('button', { 'class': 'cbi-button cbi-button-action', 'click': function(ev) {
					ev.preventDefault(); search();
				} }, _('Cari')), out ]);
		};

		o = s.option(form.ListValue, 'unit', _('Satuan suhu'));
		o.value('c', _('Celsius (°C)'));
		o.value('f', _('Fahrenheit (°F)'));
		o.default = 'c';

		o = s.option(form.Value, 'refresh', _('Perbarui data cuaca tiap'), _('Menit (1 – 180). Default 15.'));
		o.datatype = 'range(1,180)';
		o.default = '15';
		o.placeholder = '15';

		/* ---------- Scene running text ---------- */
		s = m.section(form.NamedSection, 'ticker', 'scene', _('Scene Running Text (layar penuh)'));
		s.addremove = false;

		o = s.option(form.Flag, 'enable', _('Aktifkan scene'));
		o.default = '1';

		o = s.option(form.Value, 'duration', _('Durasi tampil'), _('Detik. Default 15.'));
		o.datatype = 'range(3,3600)';
		o.default = '15';
		o.placeholder = '15';

		o = s.option(form.ListValue, 'style', _('Gaya tampilan'));
		o.value('cyber', _('Cyberpunk (equalizer, bingkai, progress)'));
		o.value('plain', _('Polos (teks besar saja)'));
		o.default = 'cyber';

		o = s.option(form.Value, 'text1', _('Teks 1 (label)'), _('Maks. 24 karakter; otomatis huruf besar. Pada gaya cyberpunk tampil sebagai kotak putih.'));
		o.maxlength = 24;
		o.placeholder = 'DEJEDE';

		o = s.option(form.Value, 'text2', _('Teks 2 (tengah)'));
		o.maxlength = 24;
		o.placeholder = 'PHOTO & VIDEO';

		o = s.option(form.Value, 'text3', _('Teks 3 (nomor / info)'));
		o.maxlength = 24;
		o.placeholder = '085236578999';

		o = s.option(form.Value, 'speed', _('Kecepatan running text'), _('Piksel per detik (10 – 150).'));
		o.datatype = 'range(10,150)';
		o.default = '50';
		o.placeholder = '50';

		o = s.option(form.Flag, 'glitch', _('Efek glitch'));
		o.default = '1';
		o.depends('style', 'cyber');

		return m.render().then(L.bind(function(mapEl) {
			return E('div', {}, [ this.renderStatus(), mapEl ]);
		}, this));
	}
});
