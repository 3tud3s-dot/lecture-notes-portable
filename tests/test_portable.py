"""Portable setup checks with fake device metadata; never record or call APIs."""
import json
import http.client
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lecture_asr.portable_device import DeviceSelectionError, selected_input
from lecture_asr.live import LiveController
from lecture_asr.local_server import make_server

ROOT=Path(__file__).resolve().parents[1]

class PortableTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.sd=SimpleNamespace(default=SimpleNamespace(device=(2,0)),
            query_devices=lambda index:{'name':'Built-in input','max_input_channels':1,'hostapi':0,'default_samplerate':48000} if index==2 else {'name':'External input','max_input_channels':1,'hostapi':1,'default_samplerate':44100} if index==4 else {'name':'Output','max_input_channels':0,'hostapi':0,'default_samplerate':44100},
            query_hostapis=lambda index:{'name':'Core Audio' if index==0 else 'Other API'})
        patcher=patch.dict(sys.modules,{'sounddevice':self.sd});patcher.start();self.addCleanup(patcher.stop)
        env=patch.dict(os.environ,{},clear=False)
        env.start();self.addCleanup(env.stop)
        os.environ.pop('LECTURE_INPUT_DEVICE',None)

    def test_system_default_is_reported_without_opening_stream(self):
        selected=selected_input(self.root)
        self.assertEqual(selected['device_index'],2)
        self.assertEqual(selected['device_name'],'Built-in input')
        self.assertEqual(selected['host_api'],'Core Audio')
        self.assertEqual(selected['selection_mode'],'system_default')

    def test_env_selects_exact_device_and_rejects_output_only(self):
        (self.root/'.env').write_text('LECTURE_INPUT_DEVICE=4\n')
        self.assertEqual(selected_input(self.root)['device_index'],4)
        os.environ['LECTURE_INPUT_DEVICE']='3'
        with self.assertRaises(DeviceSelectionError):selected_input(self.root)

    def test_catalog_and_browser_configuration_have_no_recording_side_effect(self):
        controller=LiveController(ROOT,enable_summary=True)
        config=controller.configuration()
        self.assertEqual(len(controller.courses),5)
        self.assertEqual(config['microphone_status'],'selected')
        self.assertEqual(config['device_index'],2)
        self.assertEqual(config['maximum_duration'],6000)
        self.assertTrue(config['summary_enabled'])
        self.assertIsNone(controller.worker)
        controller.close()

    def test_compact_references_have_only_runtime_terms(self):
        catalog=json.loads((ROOT/'references/index.json').read_text())
        self.assertEqual(len(catalog),5)
        for course in catalog:
            source=json.loads((ROOT/course['reference_path']).read_text())
            self.assertEqual(source['course_id'],course['course_id'])
            self.assertTrue(all('section_id' in section and 'terms' in section for section in source['sections']))
            self.assertNotIn('source_pdf',source)

    def test_local_backend_health_and_idle_state_without_recording(self):
        controller=LiveController(ROOT,enable_summary=True)
        server=make_server(controller,0)
        worker=threading.Thread(target=server.serve_forever,daemon=True)
        worker.start()
        try:
            connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=2)
            connection.request('GET','/api/health')
            response=connection.getresponse()
            self.assertEqual(response.status,200)
            health=json.loads(response.read())
            connection.close()
            self.assertTrue(health['ready'])
            self.assertEqual(health['configuration']['device_name'],'Built-in input')
            self.assertEqual(health['configuration']['maximum_duration'],6000)
            self.assertEqual(controller.snapshot()['status'],'Idle')
            self.assertIsNone(controller.worker)
        finally:
            server.exiting=True
            server.shutdown()
            server.server_close()
            worker.join(2)
            controller.close()

if __name__=='__main__':unittest.main()
