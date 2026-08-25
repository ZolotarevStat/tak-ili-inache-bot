from __future__ import annotations
import unittest
from tak_ili_inache.delivery import DeliveryPolicy, UnknownDeliveryError

class DeliveryTests(unittest.TestCase):
    def test_429_retries_but_unknown_delivery_never_retries(self):
        calls=[]
        class E(Exception): status=429
        def transient():
            calls.append(1)
            if len(calls)==1: raise E()
            return "ok"
        self.assertEqual(DeliveryPolicy(sleep=lambda _: None).send(transient), "ok")
        self.assertEqual(len(calls), 2)
        unknown=[]
        with self.assertRaises(UnknownDeliveryError): DeliveryPolicy(sleep=lambda _: None).send(lambda: unknown.append(1) or (_ for _ in ()).throw(UnknownDeliveryError()))
        self.assertEqual(len(unknown), 1)
