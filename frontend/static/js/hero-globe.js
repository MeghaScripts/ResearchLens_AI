// hero-globe.js — real geography earth, rim-glow atmosphere, orbiting light rings, publication network
(function () {
  const mount = document.getElementById('heroGlobe');
  if (!mount || typeof THREE === 'undefined') return;

  // No forced square anymore — the container is the full hero section (rectangular)

  const RADIUS = 2.3; // larger now that the container is the full hero width, not a small square

  const HUBS = [
    { name: 'Bengaluru', lat: 12.97, lon: 77.59, color: 0x00d4ff },
    { name: 'Boston',    lat: 42.36, lon: -71.06, color: 0x7c3aed },
    { name: 'London',    lat: 51.51, lon: -0.13,  color: 0x00d4ff },
    { name: 'Berlin',    lat: 52.52, lon: 13.40,  color: 0x06ffa5 },
    { name: 'Beijing',   lat: 39.90, lon: 116.40, color: 0x7c3aed },
    { name: 'Tokyo',     lat: 35.68, lon: 139.69, color: 0x00d4ff },
    { name: 'SFBay',     lat: 37.77, lon: -122.42,color: 0x06ffa5 },
    { name: 'Singapore', lat: 1.35,  lon: 103.82, color: 0x00d4ff },
    { name: 'Sydney',    lat: -33.87,lon: 151.21, color: 0x7c3aed }
  ];
  const GAP = { name: 'Gap Detected', lat: 4.0, lon: 21.5, color: 0xff9f43 };

  function latLonToVec3(lat, lon, radius) {
    const phi = (90 - lat) * (Math.PI / 180);
    const theta = (lon + 180) * (Math.PI / 180);
    return new THREE.Vector3(
      -radius * Math.sin(phi) * Math.cos(theta),
      radius * Math.cos(phi),
      radius * Math.sin(phi) * Math.sin(theta)
    );
  }

  function makeArc(a, b) {
    const mid = a.clone().add(b).multiplyScalar(0.5);
    mid.normalize().multiplyScalar(RADIUS * (1.22 + Math.random() * 0.15));
    const curve = new THREE.QuadraticBezierCurve3(a, mid, b);
    const geo = new THREE.BufferGeometry().setFromPoints(curve.getPoints(48));
    const mat = new THREE.LineBasicMaterial({ color: 0x00d4ff, transparent: true, opacity: 0.4 });
    return new THREE.Line(geo, mat);
  }

  function makeMarker(vec, color, size) {
    const group = new THREE.Group();
    const core = new THREE.Mesh(new THREE.SphereGeometry(size, 12, 12), new THREE.MeshBasicMaterial({ color }));
    const glow = new THREE.Mesh(new THREE.SphereGeometry(size * 2.8, 12, 12), new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.28 }));
    group.add(core, glow);
    group.position.copy(vec);
    return group;
  }

  let width = mount.clientWidth || 600;
  let height = mount.clientHeight || 600;

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(38, width / height, 0.1, 100);
  camera.position.set(0, 0, 4.8);

  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setSize(width, height);
  mount.appendChild(renderer.domElement);

  const globeGroup = new THREE.Group();
  scene.add(globeGroup);

  // Real Earth — a proper day/night shader: city lights only show on the dark side,
  // fading naturally at the terminator, instead of glowing uniformly everywhere.
  const loader = new THREE.TextureLoader();
  const dayTex = loader.load('/static/img/earth_diffuse.jpg');
  const lightsTex = loader.load('/static/img/earth_lights.jpg');
  const earthMat = new THREE.ShaderMaterial({
    uniforms: {
      dayTexture: { value: dayTex },
      nightTexture: { value: lightsTex },
      sunDirection: { value: new THREE.Vector3(1, 0.35, 0.9).normalize() }
    },
    vertexShader: `
      varying vec2 vUv;
      varying vec3 vWorldNormal;
      void main() {
        vUv = uv;
        vWorldNormal = normalize(mat3(modelMatrix) * normal);
        gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
      }
    `,
    fragmentShader: `
      uniform sampler2D dayTexture;
      uniform sampler2D nightTexture;
      uniform vec3 sunDirection;
      varying vec2 vUv;
      varying vec3 vWorldNormal;
      void main() {
        vec3 dayColor = texture2D(dayTexture, vUv).rgb;
        vec3 nightColor = texture2D(nightTexture, vUv).rgb * 0.55;
        float lightAmount = dot(normalize(vWorldNormal), normalize(sunDirection));
        float dayMix = smoothstep(-0.12, 0.18, lightAmount);
        vec3 shadedDay = dayColor * (0.45 + 0.65 * max(lightAmount, 0.0));
        vec3 color = mix(nightColor, shadedDay, dayMix);
        gl_FragColor = vec4(color, 1.0);
      }
    `
  });
  const core = new THREE.Mesh(new THREE.SphereGeometry(RADIUS, 64, 64), earthMat);
  globeGroup.add(core);

  // Hub markers + connecting arcs (publication network)
  const hubVecs = HUBS.map(h => latLonToVec3(h.lat, h.lon, RADIUS));
  HUBS.forEach((h, i) => globeGroup.add(makeMarker(hubVecs[i], h.color, 0.044)));
  for (let i = 0; i < HUBS.length; i++) {
    const j = (i + 3) % HUBS.length;
    globeGroup.add(makeArc(hubVecs[i], hubVecs[j]));
  }
  globeGroup.add(makeArc(hubVecs[0], hubVecs[1]));
  globeGroup.add(makeArc(hubVecs[2], hubVecs[5]));

  const gapVec = latLonToVec3(GAP.lat, GAP.lon, RADIUS);
  const gapMarker = makeMarker(gapVec, GAP.color, 0.055);
  globeGroup.add(gapMarker);

  globeGroup.rotation.x = 0.24;

  // Orbiting light rings — small bright points sweeping around the globe like satellite passes
  const orbitGroups = [];
  const ringDefs = [
    { r: RADIUS * 1.55, tilt: 0.35, count: 3, color: 0x00d4ff, speed: 0.55 },
    { r: RADIUS * 1.75, tilt: -0.5, count: 2, color: 0xff9f43, speed: -0.4 },
    { r: RADIUS * 1.9,  tilt: 1.1,  count: 2, color: 0x7c3aed, speed: 0.32 }
  ];
  ringDefs.forEach(def => {
    const ringPts = [];
    for (let i = 0; i <= 64; i++) {
      const a = (i / 64) * Math.PI * 2;
      ringPts.push(new THREE.Vector3(Math.cos(a) * def.r, 0, Math.sin(a) * def.r));
    }
    const ringGeo = new THREE.BufferGeometry().setFromPoints(ringPts);
    const ringLine = new THREE.Line(ringGeo, new THREE.LineBasicMaterial({ color: def.color, transparent: true, opacity: 0.15 }));
    ringLine.rotation.x = def.tilt;
    scene.add(ringLine);

    const dots = [];
    for (let i = 0; i < def.count; i++) {
      const dot = new THREE.Mesh(new THREE.SphereGeometry(0.025, 8, 8), new THREE.MeshBasicMaterial({ color: def.color }));
      const glow = new THREE.Mesh(new THREE.SphereGeometry(0.07, 8, 8), new THREE.MeshBasicMaterial({ color: def.color, transparent: true, opacity: 0.35 }));
      dot.add(glow);
      scene.add(dot);
      dots.push({ mesh: dot, offset: (i / def.count) * Math.PI * 2 });
    }
    orbitGroups.push({ def, dots });
  });

  function onResize() {
    width = mount.clientWidth || width;
    height = mount.clientHeight || height;
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
    renderer.setSize(width, height);
  }
  window.addEventListener('resize', onResize);

  let t = 0;
  function animate() {
    requestAnimationFrame(animate);
    t += 0.016;
    globeGroup.rotation.y += 0.0018;
    gapMarker.scale.setScalar(1 + Math.sin(t * 2.2) * 0.35);

    orbitGroups.forEach(g => {
      g.dots.forEach(d => {
        const a = t * g.def.speed + d.offset;
        const x = Math.cos(a) * g.def.r;
        const z = Math.sin(a) * g.def.r;
        const y = 0;
        const tilt = g.def.tilt;
        d.mesh.position.set(
          x,
          y * Math.cos(tilt) - z * Math.sin(tilt),
          y * Math.sin(tilt) + z * Math.cos(tilt)
        );
      });
    });

    renderer.render(scene, camera);
  }
  animate();
})();